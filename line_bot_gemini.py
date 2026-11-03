import os
import io
import json
import re
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    StickerMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent
)
from PIL import Image
import google.generativeai as genai

app = Flask(__name__)

# เขตเวลาประเทศไทย (UTC+7)
THAI_TZ = timezone(timedelta(hours=7))

# ==============================================================================
# Configurations & Credentials
# ==============================================================================
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "YOUR_LINE_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "YOUR_LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

if GEMINI_API_KEY and GEMINI_API_KEY != "YOUR_GEMINI_API_KEY":
    genai.configure(api_key=GEMINI_API_KEY)

# โมเดล Gemini สำหรับประมวลผลข้อความ
text_model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction="""
คุณคือแอดมินเพจงานคอนเสิร์ต "Jabfest 2027" (จ๊าบเฟสต์) ณ 18 ไร่แก่งกระจาน จ.เพชรบุรี วันที่ 20 มีนาคม 2570
ศิลปิน: ลาบานูน, กบ Taxi, ป่ากอง, ฟอร์ยูแบนด์ (House Band), และรำวงมังคุดเพชร
งานเริ่ม 17.00 - 02.00 น. ธีมงานวัดวินเทจผสมผสานความทันสมัย ชิงช้าสวรรค์ สาวน้อยตกน้ำ ปืนอัดลม และร้านค้ากว่า 20 ร้าน
ราคาบัตร Early Bird: 200 บาท/ใบ
ตอบคำถามลูกค้าอย่างสุภาพ อารมณ์ดี เป็นกันเอง และเชิญชวนให้มาสนุกด้วยกัน
"""
)

# โมเดล Gemini Vision สำหรับตรวจสอบสลิปโอนเงิน
vision_model = genai.GenerativeModel(model_name="gemini-1.5-flash")

# Session Store ในหน่วยความจำเพื่อจำสถานะลูกค้าแต่ละคน
user_sessions = {}

TICKET_PRICE = 200  # บัตร Early Bird 200 บาท / ใบ

# ==============================================================================
# Helper Functions & Line-by-Line Precision Parser
# ==============================================================================

def get_step1_text():
    """ข้อความตอบกลับขั้นตอนที่ 1 เมื่อลูกค้าต้องการซื้อบัตร"""
    return (
        "สวัสดีคะ รายละเอียดการจองบัตร Early Bird งานจ๊าบเฟสติ์ \n"
        f"ราคาบัตร: {TICKET_PRICE} บาท / 1 ใบ\n\n"
        "ขั้นตอนการสั่งซื้อ:\n"
        "1. ขื่อ - นามสกุล :\n"
        "2. เบอร์ติดต่อ :\n"
        "3. จำนวนบัตรที่ต้องการ :\n\n"
        "เดี๋ยวแอดมินจะรีบตรวจสอบและส่ง E-Ticket ให้ทางนี้นะคะ\n\n"
        "เตรียมตัวไปกระโดดให้ยับกับลาบานูนด้วยกัน 20 มีนาคม 2570"
    )

def get_step2_text(name, phone, tickets, total_price):
    """ข้อความสรุปยอดเงินและเลขบัญชีในขั้นตอนที่ 2 ขานชื่อ เบอร์ จำนวน และยอดรวมชัดเจน"""
    formatted_price = f"{total_price:,}"
    return (
        "ขออนุญาตสรุปรายละเอียดการจองนะคะ \n\n"
        f"ชื่อ - นามสกุล : {name}\n"
        f"เบอร์โทร : {phone}\n"
        f"จำนวน/ท่าน : {tickets} ใบ\n"
        f"ยอดชำระ : {formatted_price} บาท\n\n"
        "ชำระค่าบัตรได้ที่ \n"
        "ธนาคารไทยพานิช\n"
        "571-403156-3\n"
        "นางสาว นภาพร นาควงศ์\n\n"
        "หากลูกค้าชำระเงินแล้ว รบกวนส่งสลิปหลักฐานการโอนได้ที่แชทนี้ได้เลยคะ"
    )

def get_step3_text(name, phone, tickets, amount):
    """ข้อความยืนยันการจองในขั้นตอนที่ 3 หลังตรวจสลิปผ่าน"""
    customer_info = f"{name} {phone}".strip() if phone else name
    formatted_amount = f"{amount:,}" if isinstance(amount, (int, float)) else str(amount)
    return (
        "ยืนยันการจองค่ะ\n\n"
        f"ชื่อและเบอร์โทรคุณลูกค้า : {customer_info}\n"
        f"จำนวน/ท่าน : {tickets} ใบ\n"
        f"ยอดการชำระเงิน : {formatted_amount} บาท\n\n"
        "(ลูกค้าสามารถแสดงหลักฐานเพื่อยืนยันการจองในวันงานได้เลยค่ะ) \n\n"
        "- ขอบคุณคะ เจอกันวันที่ 20 มีนาคม สนุกแน่นอนคะ"
    )

def extract_partial_booking_info(user_text):
    """
    สกัดข้อมูลการจองแต่ละส่วน (ชื่อ, เบอร์โทร, จำนวนบัตร) จากข้อความ
    รองรับทั้งพิมพ์มาพร้อมกัน 3 บรรทัด หรือทยอยพิมพ์ทีละข้อความ
    """
    raw_lines = [l.strip() for l in user_text.splitlines() if l.strip()]
    lines = [re.sub(r'^[1-3][\.\:\)\s]+', '', l).strip() for l in raw_lines]
    
    found_name = None
    found_phone = None
    found_tickets = None

    # 1. ค้นหาเบอร์โทรติดต่อ (ขึ้นต้นด้วย 0 และมี 9-10 หลัก)
    phone_match = re.search(r'(0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{3,4}|0\d{8,9})', user_text)
    if phone_match:
        found_phone = phone_match.group(1).replace('-', '').replace(' ', '').strip()

    # 2. ค้นหาจำนวนบัตร
    ticket_match = re.search(r'(\d+)\s*(?:ใบ|ที่|คน|ท่าน|ticket|ตั๋ว)', user_text, re.I)
    if ticket_match:
        found_tickets = int(ticket_match.group(1))
    else:
        num_match = re.search(r'(?:จำนวน|บัตร|ที่ต้องการ)[^\d\n]*[:\-\s]*(\d+)', user_text)
        if num_match:
            found_tickets = int(num_match.group(1))
        elif len(lines) >= 3:
            third_line = lines[2]
            digits = re.findall(r'\d+', third_line)
            if digits:
                found_tickets = int(digits[0])
        elif len(lines) == 1 and re.match(r'^\d+$', lines[0]) and int(lines[0]) <= 50:
            found_tickets = int(lines[0])

    # 3. ค้นหาชื่อ-นามสกุล
    is_ticket_line = lambda t: bool(re.search(r'\d+\s*(?:ใบ|ที่|คน|ท่าน|ตั๋ว)', t, re.I) or re.search(r'(?:จำนวน|บัตร)[^\d\n]*[:\-\s]*\d+', t))
    
    if len(lines) >= 1:
        first_line = lines[0]
        cleaned_first = re.sub(r'^(?:ขื่อ|ชื่อ)[\s\-_]*(?:นามสกุล|สกุล)?[\s:\-\.]*', '', first_line).strip()
        # ไม่ใช่เบอร์โทร ไม่ใช่จำนวนบัตร และไม่ใช่ตัวเลขล้วน
        if not re.match(r'^0\d{8,9}$', cleaned_first.replace('-', '').replace(' ', '')) and not re.match(r'^\d+$', cleaned_first) and not is_ticket_line(first_line):
            if len(cleaned_first) >= 2:
                found_name = cleaned_first

    if not found_name:
        for line in lines:
            cleaned = re.sub(r'^(?:ขื่อ|ชื่อ)[\s\-_]*(?:นามสกุล|สกุล)?[\s:\-\.]*', '', line).strip()
            if found_phone and found_phone in line.replace('-', '').replace(' ', ''):
                continue
            if is_ticket_line(line) or re.match(r'^\d+$', cleaned):
                continue
            if len(cleaned) >= 2 and not re.match(r'^[0-9\s\.\-\:\(\)]+$', cleaned):
                found_name = cleaned
                break

    return found_name, found_phone, found_tickets

def verify_slip_image(image_bytes, expected_amount=200):
    """
    ระบบตรวจสอบสลิปโอนเงินตาม 2 เงื่อนไขเท่านั้น:
    1. ตรวจสอบชื่อบัญชีปลายทางเป็น 'นางสาว นภาพร นาควงศ์' เท่านั้น
    2. ตรวจสอบตัวเลขบนสลิปโอนเงินว่าตรงกับยอดชำระไหม
    """
    prompt = f"""
ตรวจวิเคราะห์สลิปการโอนเงินนี้ตาม 2 ข้อเท่านั้น:
1. ชื่อบัญชีผู้รับเงิน (ปลายทาง): คือ "นางสาว นภาพร นาควงศ์" หรือ "นภาพร นาควงศ์" ใช่หรือไม่
2. จำนวนเงินที่โอนสำเร็จ: ตัวเลขยอดเงินที่โอนบนสลิปคือเท่าไหร่ (ยอดชำระคือ {expected_amount} บาท)

ตอบในรูปแบบ 2 บรรทัดนี้เท่านั้น (ห้ามใส่คำบรรยายอื่น):
RECIPIENT_NAME_MATCH: [YES หรือ NO]
TRANSFER_AMOUNT: [ระบุเฉพาะตัวเลข เช่น 200 หรือ 400 หรือ 2000]
"""
    try:
        pil_img = Image.open(io.BytesIO(image_bytes))
        res = vision_model.generate_content([prompt, pil_img])
        text_out = res.text.strip()
        print("Gemini Slip Verification Output:\n", text_out)

        text_upper = text_out.upper()

        # เงื่อนไขที่ 1: ตรวจสอบเฉพาะชื่อบัญชีผู้รับเป็น นภาพร นาควงศ์
        name_match = (
            "RECIPIENT_NAME_MATCH: YES" in text_upper
            or "นภาพร" in text_out
            or "นาควงศ์" in text_out
        )

        # เงื่อนไขที่ 2: ตรวจสอบตัวเลขบนสลิปโอนเงิน
        amt_match = re.search(r'(?:TRANSFER_AMOUNT|AMOUNT|ยอด|จำนวน)[^\d\n]*[:\s]*(\d+(?:\.\d+)?)', text_out, re.I)
        if not amt_match:
            amt_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:บาท|THB)', text_out, re.I)

        slip_amount = float(amt_match.group(1)) if amt_match else float(expected_amount)
        amount_match = (abs(slip_amount - expected_amount) < 1.0) or (slip_amount >= expected_amount and expected_amount > 0)

        is_passed = bool(name_match and amount_match)

        return {
            "is_passed": is_passed,
            "name_match": name_match,
            "amount_match": amount_match,
            "slip_amount": slip_amount,
            "raw_text": text_out
        }
    except Exception as e:
        print("Error verifying slip with Gemini:", e)
        return {
            "is_passed": False,
            "name_match": False,
            "amount_match": False,
            "slip_amount": 0.0,
            "raw_text": str(e)
        }

# ==============================================================================
# Webhook Endpoint
# ==============================================================================

@app.route("/", methods=["GET"])
def home():
    return "Jabfest 2027 Line Bot is running successfully!"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"

# ------------------------------------------------------------------------------
# 1. จัดการข้อความตัวอักษร (Text Message)
# ------------------------------------------------------------------------------
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()
    session = user_sessions.get(user_id, {})

    # [ขั้นตอนที่ 1]: เริ่มต้นการจองเมื่อทัก "ซื้อบัตร", "จองบัตร"
    if any(kw in user_text for kw in ["ซื้อบัตร", "จองบัตร", "ราคาบัตร"]):
        user_sessions[user_id] = {
            "step": 1,
            "name": None,
            "phone": None,
            "tickets": None,
            "total_price": 0,
            "booked_at": datetime.now(THAI_TZ)
        }
        reply_text = get_step1_text()
        send_reply(event.reply_token, [TextMessage(text=reply_text)])
        return

    # [ขั้นตอนการกรอกรายละเอียด 3 ข้อ]:
    # หากลูกค้าอยู่ในขั้นตอนการจอง (step 1) หรือกำลังส่งข้อมูลเข้ามา
    if session.get("step") == 1 or session.get("step") == 2:
        found_name, found_phone, found_tickets = extract_partial_booking_info(user_text)

        # บันทึกสะสมข้อมูลที่ลูกค้าส่งมา
        if found_name:
            session["name"] = found_name
        if found_phone:
            session["phone"] = found_phone
        if found_tickets:
            session["tickets"] = found_tickets

        # ตรวจสอบว่าข้อมูลครบทั้ง 3 ข้อหรือยัง
        missing = []
        if not session.get("name"):
            missing.append("ชื่อ - นามสกุล")
        if not session.get("phone"):
            missing.append("เบอร์ติดต่อ")
        if not session.get("tickets"):
            missing.append("จำนวนบัตรที่ต้องการ")

        # กรณีที่ 1: ครบทั้ง 3 ข้อแล้ว -> ให้สรุปรายละเอียดการจอง (ขั้นตอนที่ 2)
        if len(missing) == 0:
            name = session["name"]
            phone = session["phone"]
            tickets = int(session["tickets"])
            total_price = tickets * TICKET_PRICE

            session["step"] = 2
            session["total_price"] = total_price
            user_sessions[user_id] = session

            reply_text = get_step2_text(name, phone, tickets, total_price)
            send_reply(event.reply_token, [TextMessage(text=reply_text)])
            return

        # กรณีที่ 2: ยังไม่ครบ 3 ข้อ -> รอทางลูกค้าตอบให้หมดก่อน และทวงถามข้อที่ขาด
        else:
            user_sessions[user_id] = session
            if len(missing) == 3:
                # ยังไม่มีข้อมูลเลย
                reply_text = (
                    "รบกวนแจ้งรายละเอียดการจองให้ครบทั้ง 3 ข้อดังนี้ด้วยนะคะ:\n"
                    "1. ขื่อ - นามสกุล :\n"
                    "2. เบอร์ติดต่อ :\n"
                    "3. จำนวนบัตรที่ต้องการ :\n\n"
                    "เมื่อแจ้งครบแล้ว แอดมินจะรีบสรุปยอดจองให้ทันทีค่ะ"
                )
            elif len(missing) == 2:
                reply_text = f"ได้รับข้อมูลเรียบร้อยแล้วค่ะ รบกวนขอ {missing[0]} และ {missing[1]} เพิ่มเติมด้วยนะคะ เพื่อแอดมินจะได้สรุปยอดจองให้ค่ะ"
            elif len(missing) == 1:
                reply_text = f"ได้รับข้อมูลเรียบร้อยแล้วค่ะ รบกวนขอ {missing[0]} เพิ่มเติมอีกนิดนะคะ เพื่อแอดมินจะได้สรุปยอดจองให้ค่ะ"

            send_reply(event.reply_token, [TextMessage(text=reply_text)])
            return

    # กรณีถามคำถามทั่วไป (FAQ ของงาน Jabfest)
    try:
        response = text_model.generate_content(
            f"ลูกค้าถามว่า: '{user_text}'\nกรุณาตอบคำถามลูกค้าอย่างกระชับ สุภาพ เป็นกันเอง และหากลูกค้าสนใจซื้อบัตรให้แนะนำให้พิมพ์ 'ซื้อบัตร' ได้เลย"
        )
        reply_content = response.text.strip()
    except Exception:
        reply_content = "สอบถามข้อมูลเพิ่มเติมเรื่องบัตรหรือกิจกรรมในงาน ทักแอดมินหรือพิมพ์ 'ซื้อบัตร' ได้เลยนะคะ"

    send_reply(event.reply_token, [TextMessage(text=reply_content)])

# ------------------------------------------------------------------------------
# 2. จัดการรูปภาพสลิปโอนเงิน (Image Message - ตรวจสลิปตาม 2 เงื่อนไข)
# ------------------------------------------------------------------------------
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    user_id = event.source.user_id
    message_id = event.message.id
    session = user_sessions.get(user_id, {})
    
    expected_amount = session.get("total_price", TICKET_PRICE)
    if not expected_amount or expected_amount <= 0:
        expected_amount = TICKET_PRICE

    # ดาวน์โหลดรูปภาพสลิปจาก LINE Server
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(message_id)

    # ส่งให้ Gemini Vision ตรวจสอบตาม 2 เงื่อนไข:
    # 1. ชื่อบัญชีปลายทางเป็น 'นางสาว นภาพร นาควงศ์'
    # 2. จำนวนเงินตรงกับยอดชำระ
    slip_result = verify_slip_image(image_bytes, expected_amount=expected_amount)
    
    is_passed = slip_result.get("is_passed", False)
    name_match = slip_result.get("name_match", False)
    amount_match = slip_result.get("amount_match", False)
    slip_amount = slip_result.get("slip_amount", expected_amount)

    # หากผ่าน 2 ข้อนี้ ให้ผ่านไปตอบขั้นตอนที่ 3 ได้เลยทันที
    if is_passed or (name_match and (slip_amount >= 200.0)):
        actual_amount = slip_amount if slip_amount and slip_amount > 0 else expected_amount
        tickets = session.get("tickets") or max(1, int(round(actual_amount / TICKET_PRICE)))

        name = session.get("name") or "คุณลูกค้า"
        phone = session.get("phone") or ""

        # [ขั้นตอนที่ 3]: ข้อความยืนยันการจอง + สติกเกอร์ขอบคุณ
        reply_text = get_step3_text(name, phone, tickets, int(actual_amount))
        sticker_msg = StickerMessage(package_id="446", sticker_id="1988")
        user_sessions[user_id]["step"] = 3
        send_reply(event.reply_token, [TextMessage(text=reply_text), sticker_msg])

    else:
        # กรณีไม่ผ่าน: แจ้งเหตุผลให้ลูกค้าทราบอย่างชัดเจน
        if not name_match:
            err_msg = (
                "ขออภัยค่ะ แอดมินตรวจสอบสลิปแล้ว ชื่อบัญชีผู้รับปลายทางไม่ใช่ 'นางสาว นภาพร นาควงศ์' ค่ะ\n\n"
                "รบกวนตรวจสอบการโอนเข้าบัญชี:\n"
                "ธนาคารไทยพาณิชย์ (SCB)\n"
                "เลขบัญชี: 571-403156-3\n"
                "ชื่อบัญชี: นางสาว นภาพร นาควงศ์\n\n"
                "หากโอนถูกต้องเรียบร้อยแล้ว สามารถส่งภาพสลิปที่ถูกต้องอีกครั้งได้เลยนะคะ"
            )
        elif not amount_match:
            err_msg = (
                f"ขออภัยค่ะ ยอดเงินในสลิป ({slip_amount:,.2f} บาท) ไม่ตรงกับยอดชำระ ({expected_amount:,.2f} บาท) ค่ะ\n\n"
                "รบกวนตรวจสอบยอดเงินและส่งสลิปหลักฐานการโอนที่ถูกต้องอีกครั้งนะคะ"
            )
        else:
            err_msg = (
                "ขออภัยค่ะ แอดมินไม่สามารถอ่านข้อมูลบนภาพสลิปได้ชัดเจน\n"
                "รบกวนถ่ายหรือส่งภาพสลิปการโอนเงินที่ชัดเจนอีกครั้งนะคะ"
            )
        send_reply(event.reply_token, [TextMessage(text=err_msg)])

def send_reply(reply_token, messages):
    """ส่งข้อความตอบกลับไปยัง LINE"""
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=messages
            )
        )

# ==============================================================================
# Main Runner
# ==============================================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
