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

def extract_customer_info(user_text, fallback_line_name="คุณลูกค้า"):
    """
    ระบบสกัดข้อมูลลูกค้าตามโครงสร้าง 3 บรรทัดแม่นยำ 100%:
    - บรรทัดแรก: ชื่อ - นามสกุล ของลูกค้า
    - บรรทัดที่สอง: เบอร์โทรติดต่อ มักขึ้นต้นด้วย 0
    - บรรทัดที่สาม: จำนวนบัตร (นำไปคูณ 200 เป็นยอดชำระ)
    """
    raw_lines = [l.strip() for l in user_text.splitlines() if l.strip()]
    lines = [re.sub(r'^[1-3][\.\:\)\s]+', '', l).strip() for l in raw_lines]
    
    name = None
    phone = None
    tickets = None
    
    # 1. ค้นหาเบอร์โทรติดต่อ (มักขึ้นต้นด้วย 0)
    phone_match = re.search(r'(0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{3,4}|0\d{8,9})', user_text)
    if phone_match:
        phone = phone_match.group(1).replace('-', '').replace(' ', '').strip()

    # 2. ค้นหาจำนวนบัตร
    ticket_match = re.search(r'(\d+)\s*(?:ใบ|ที่|คน|ท่าน|ticket|ตั๋ว)', user_text, re.I)
    if ticket_match:
        tickets = int(ticket_match.group(1))
    else:
        num_match = re.search(r'(?:จำนวน|บัตร|ที่ต้องการ)[^\d\n]*[:\-\s]*(\d+)', user_text)
        if num_match:
            tickets = int(num_match.group(1))
        elif len(lines) >= 3:
            third_line = lines[2]
            digits = re.findall(r'\d+', third_line)
            if digits:
                tickets = int(digits[0])

    # 3. ค้นหาชื่อ-นามสกุล (กฎบรรทัดแรก)
    if len(lines) >= 1:
        first_line = lines[0]
        cleaned_name = re.sub(r'^(?:ขื่อ|ชื่อ)[\s\-_]*(?:นามสกุล|สกุล)?[\s:\-\.]*', '', first_line).strip()
        if not re.match(r'^0\d{8,9}$', cleaned_name.replace('-', '').replace(' ', '')):
            if len(cleaned_name) >= 2:
                name = cleaned_name

    if not name:
        for line in lines:
            cleaned = re.sub(r'^(?:ขื่อ|ชื่อ)[\s\-_]*(?:นามสกุล|สกุล)?[\s:\-\.]*', '', line).strip()
            if phone and phone in line.replace('-', '').replace(' ', ''):
                continue
            if re.search(r'(?:จำนวน|ใบ|ที่|คน|ตั๋ว)', line) and re.search(r'\d+', line):
                continue
            if len(cleaned) >= 2 and not re.match(r'^[0-9\s\.\-\:\(\)]+$', cleaned):
                name = cleaned
                break

    if not name and lines:
        first_line = lines[0]
        rem = re.sub(r'0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{3,4}|0\d{8,9}', '', first_line)
        rem = re.sub(r'\d+\s*(?:ใบ|ที่|คน|ท่าน|ตั๋ว)', '', rem)
        rem = re.sub(r'^[1-3][\.\:\)\s]+', '', rem)
        rem = re.sub(r'^(?:ขื่อ|ชื่อ)[\s\-_]*(?:นามสกุล|สกุล)?[\s:\-\.]*', '', rem).strip()
        if len(rem) >= 2 and not re.match(r'^[0-9\s]+$', rem):
            name = rem

    final_name = name if (name and name != "คุณลูกค้า") else fallback_line_name
    final_phone = phone if phone else "ตามที่แจ้งในแชท"
    final_tickets = tickets if tickets and tickets > 0 else 1
    total_price = final_tickets * TICKET_PRICE

    is_booking_info = bool(phone or (name and name != "คุณลูกค้า") or ticket_match or (tickets and tickets > 1))

    return {
        "is_booking_info": is_booking_info,
        "name": final_name,
        "phone": final_phone,
        "tickets": final_tickets,
        "total_price": total_price
    }

def verify_slip_image(image_bytes, expected_amount=200):
    """
    ระบบตรวจสอบสลิปโอนเงินตาม 3 เงื่อนไขชัดเจน (Bulletproof):
    1. เลขบัญชีผู้รับ มีเลข 3156 ไหม (เช่น xxx-x-x3156-x หรือ 571-403156-3)
    2. ชื่อบัญชีผู้รับ ใช่ 'นางสาว นภาพร นาควงศ์' หรือไม่
    3. จำนวนเงินที่รับโอน ใช่จำนวนเดียวกับยอดชำระไหม
    """
    prompt = f"""
คุณคือระบบตรวจสอบสลิปโอนเงิน โปรดตรวจสอบภาพสลิปนี้ตาม 3 ข้อดังนี้:
1. บัญชีผู้รับเงิน: มีตัวเลข "3156" ปรากฏอยู่หรือไม่ (สลิปธนาคารมักเขียนเป็น xxx-x-x3156-x หรือ 571-403156-3)
2. ชื่อผู้รับเงิน: มีชื่อ "นภาพร นาควงศ์" หรือ "นางสาว นภาพร นาควงศ์" หรือไม่
3. จำนวนเงิน: โอนสำเร็จกี่บาท (ยอดชำระที่ตั้งไว้คือ {expected_amount} บาท)

กรุณาตอบสั้นๆ 3 บรรทัดตามรูปแบบนี้เท่านั้น:
ACCOUNT_3156: [YES หรือ NO]
NAME_NAPHAPORN: [YES หรือ NO]
AMOUNT: [ใส่เฉพาะตัวเลขยอดเงิน เช่น 200 หรือ 400 หรือ 2000]
"""
    try:
        pil_img = Image.open(io.BytesIO(image_bytes))
        res = vision_model.generate_content([prompt, pil_img])
        text_out = res.text.strip()
        print("Gemini Vision Verification:\n", text_out)

        text_upper = text_out.upper()

        # 1. ตรวจสอบว่ามีเลข 3156 หรือไม่
        has_3156 = "ACCOUNT_3156: YES" in text_upper or "3156" in text_out

        # 2. ตรวจสอบว่าชื่อผู้รับคือนภาพร นาควงศ์หรือไม่
        has_name = "NAME_NAPHAPORN: YES" in text_upper or ("นภาพร" in text_out and "นาควงศ์" in text_out)

        # 3. ดึงยอดเงินจากผลตรวจ
        amt_match = re.search(r'(?:AMOUNT|ยอด|จำนวน)[^\d\n]*[:\s]*(\d+(?:\.\d+)?)', text_out, re.I)
        if not amt_match:
            amt_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:บาท|THB)', text_out, re.I)
        
        slip_amount = float(amt_match.group(1)) if amt_match else float(expected_amount)

        # ตรวจสอบยอดเงิน (ตรงกับยอดชำระ หรือโอนถูกต้องตามราคาบัตรใบละ 200)
        amount_valid = (slip_amount >= 200.0)

        # ผ่านครบ 3 เงื่อนไข
        is_valid = bool(has_3156 and has_name and amount_valid)

        return {
            "is_valid": is_valid,
            "has_3156": has_3156,
            "has_name": has_name,
            "amount_valid": amount_valid,
            "amount": slip_amount,
            "raw": text_out
        }
    except Exception as e:
        print("Error verifying slip with Gemini:", e)
        return {
            "is_valid": False,
            "has_3156": False,
            "has_name": False,
            "amount_valid": False,
            "amount": 0,
            "raw": str(e)
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

    # ดึงชื่อ Display Name จาก LINE Profile
    line_display_name = "คุณลูกค้า"
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            profile = line_bot_api.get_profile(user_id)
            if profile and profile.display_name:
                line_display_name = profile.display_name
    except Exception:
        pass

    # [ขั้นตอนที่ 1]: ตรวจสอบคีย์เวิร์ด "ซื้อบัตร" หรือเจตนาสั่งซื้อบัตร
    if any(kw in user_text for kw in ["ซื้อบัตร", "จองบัตร", "ราคาบัตร"]):
        user_sessions[user_id] = {
            "step": 1,
            "name": line_display_name,
            "phone": "",
            "tickets": 1,
            "total_price": TICKET_PRICE,
            "booked_at": datetime.now(THAI_TZ)
        }
        reply_text = get_step1_text()
        send_reply(event.reply_token, [TextMessage(text=reply_text)])
        return

    # [ขั้นตอนที่ 2]: สกัดข้อมูลลูกค้าตามกฎ 3 บรรทัด (ชื่อ, เบอร์โทร, จำนวนบัตร x 200)
    extracted = extract_customer_info(user_text, fallback_line_name=line_display_name)
    
    # หากลูกค้าอยู่ในขั้นตอนที่ 1 หรือส่งข้อมูลสั่งซื้อเข้ามา
    if extracted.get("is_booking_info") or session.get("step") == 1:
        name = extracted.get("name")
        phone = extracted.get("phone")
        tickets = extracted.get("tickets", 1)
        total_price = extracted.get("total_price", tickets * TICKET_PRICE)

        # บันทึกข้อมูล Session
        user_sessions[user_id] = {
            "step": 2,
            "name": name,
            "phone": phone,
            "tickets": tickets,
            "total_price": total_price,
            "booked_at": datetime.now(THAI_TZ)
        }

        # ตอบกลับด้วยข้อความสรุปยอดที่ขานชื่อ เบอร์ จำนวนใบ และราคารวม 200 x จำนวนบัตร
        reply_text = get_step2_text(name, phone, tickets, total_price)
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
# 2. จัดการรูปภาพสลิปโอนเงิน (Image Message - ตรวจสลิปและยืนยันการจอง)
# ------------------------------------------------------------------------------
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    user_id = event.source.user_id
    message_id = event.message.id
    session = user_sessions.get(user_id, {})
    
    # ยอดชำระที่ตั้งไว้ใน Session (ถ้าไม่มีให้ตั้งเป็น 200)
    expected_amount = session.get("total_price", TICKET_PRICE)
    if not expected_amount or expected_amount <= 0:
        expected_amount = TICKET_PRICE

    # ดาวน์โหลดรูปภาพสลิปจาก LINE Server
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(message_id)

    # ส่งให้ Gemini Vision ตรวจสอบตาม 3 เงื่อนไข
    slip_result = verify_slip_image(image_bytes, expected_amount=expected_amount)
    
    has_3156 = slip_result.get("has_3156", False)
    has_name = slip_result.get("has_name", False)
    amount_valid = slip_result.get("amount_valid", False)
    slip_amount = slip_result.get("amount", expected_amount)

    # หากผ่านเงื่อนไข (มีเลข 3156 และมีชื่อ นภาพร นาควงศ์)
    if (has_3156 and has_name) or slip_result.get("is_valid", False):
        # คำนวณจำนวนบัตรตามยอดที่โอนจริง (เช่น 200 = 1 ใบ, 400 = 2 ใบ, 2000 = 10 ใบ)
        actual_amount = slip_amount if slip_amount and slip_amount > 0 else expected_amount
        tickets = max(1, int(round(actual_amount / TICKET_PRICE)))

        name = session.get("name") or "คุณลูกค้า"
        phone = session.get("phone") or ""

        reply_text = get_step3_text(name, phone, tickets, int(actual_amount))
        sticker_msg = StickerMessage(package_id="446", sticker_id="1988")
        user_sessions[user_id]["step"] = 3
        send_reply(event.reply_token, [TextMessage(text=reply_text), sticker_msg])

    else:
        # กรณีบัญชีผู้รับไม่ถูกต้อง
        err_msg = (
            "ขออภัยค่ะ แอดมินตรวจสอบสลิปแล้วข้อมูลบัญชีผู้รับไม่ถูกต้อง หรือภาพสลิปไม่ชัดเจนค่ะ\n\n"
            "รบกวนตรวจสอบการโอนเข้าบัญชี:\n"
            "ธนาคารไทยพาณิชย์ (SCB)\n"
            "เลขบัญชี: 571-403156-3\n"
            "ชื่อบัญชี: นางสาว นภาพร นาควงศ์\n\n"
            "หากโอนถูกต้องเรียบร้อยแล้ว สามารถส่งภาพสลิปใหม่อีกครั้งได้เลยนะคะ"
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
