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

genai.configure(api_key=GEMINI_API_KEY)

# โมเดล Gemini สำหรับประมวลผลข้อความและการสกัดข้อมูล
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
# Helper Functions for Workflow
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
    """ข้อความสรุปยอดเงินและเลขบัญชีในขั้นตอนที่ 2"""
    return (
        "ขออนุญาตสรุปรายละเอียดการจองนะคะ \n\n"
        f"ชื่อ - นามสกุล : {name}\n"
        f"เบอร์โทร : {phone}\n"
        f"จำนวน/ท่าน : {tickets} ใบ\n"
        f"ยอดชำระ : {total_price} บาท\n\n"
        "ชำระค่าบัตรได้ที่ \n"
        "ธนาคารไทยพานิช\n"
        "571-403156-3\n"
        "นางสาว นภาพร นาควงศ์\n\n"
        "หากลูกค้าชำระเงินแล้ว รบกวนส่งสลิปหลักฐานการโอนได้ที่แชทนี้ได้เลยคะ"
    )

def get_step3_text(name, phone, tickets, amount):
    """ข้อความยืนยันการจองในขั้นตอนที่ 3 หลังตรวจสลิปผ่าน"""
    customer_info = f"{name} {phone}".strip()
    return (
        "ยืนยันการจองค่ะ\n\n"
        f"ชื่อและเบอร์โทรคุณลูกค้า : {customer_info}\n"
        f"จำนวน/ท่าน : {tickets} ใบ\n"
        f"ยอดการชำระเงิน : {amount} บาท\n\n"
        "(ลูกค้าสามารถแสดงหลักฐานเพื่อยืนยันการจองในวันงานได้เลยค่ะ) \n\n"
        "- ขอบคุณคะ เจอกันวันที่ 20 มีนาคม สนุกแน่นอนคะ"
    )

def extract_customer_info(user_text):
    """ใช้ Gemini สกัด ชื่อ เบอร์โทร และจำนวนบัตร จากข้อความของลูกค้า"""
    prompt = f"""
วิเคราะห์ข้อความต่อไปนี้ของลูกค้าที่ส่งข้อมูลการจองบัตรคอนเสิร์ต:
"{user_text}"

กรุณาสกัดข้อมูลและตอบเป็น JSON ตามโครงสร้างนี้เท่านั้น (ห้ามใส่ markdown อื่น):
{{
  "is_booking_info": true/false, // true หากมีชื่อ หรือเบอร์โทร หรือจำนวนบัตร
  "name": "ชื่อ-นามสกุล หรือชื่อลูกค้า (ถ้าไม่มีให้ระบุ null)",
  "phone": "เบอร์โทรศัพท์ (ถ้าไม่มีให้ระบุ null)",
  "tickets": ตัวเลขจำนวนเต็มของจำนวนบัตร (int, ถ้าไม่มีให้ระบุ 1)
}}
"""
    try:
        res = text_model.generate_content(prompt)
        raw_text = res.text.strip()
        json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        print("Error extracting customer info:", e)
    return {"is_booking_info": False, "name": None, "phone": None, "tickets": 1}

def verify_slip_image(image_bytes, booked_at_dt=None):
    """ใช้ Gemini Vision ตรวจสอบสลิปโอนเงิน (เน้นบัญชีผู้รับ, วันที่และเวลาโอนป้องกันสลิปเก่า/ปลอม, และยอดเงิน)"""
    current_dt = datetime.now(THAI_TZ)
    current_time_str = current_dt.strftime("%Y-%m-%d %H:%M:%S")
    booked_time_str = booked_at_dt.strftime("%Y-%m-%d %H:%M:%S") if booked_at_dt else current_time_str

    prompt = f"""
คุณคือระบบตรวจสอบสลิปโอนเงินสำหรับงานคอนเสิร์ต Jabfest 2027
เวลาปัจจุบันของระบบเซิร์ฟเวอร์: {current_time_str} (เวลาประเทศไทย)
เวลาที่ลูกค้าเริ่มทำรายการสรุปยอด: {booked_time_str}

โปรดตรวจสอบสลิปโอนเงินนี้อย่างเคร่งครัดเพื่อป้องกันสลิปปลอม สลิปเก่า หรือสลิปที่นำมาใช้ซ้ำ:

1. บัญชีผู้รับเงิน (เน้นย้ำจุดนี้):
   - ต้องเป็น "ธนาคารไทยพาณิชย์" (SCB / Siam Commercial Bank)
   - เลขบัญชีคือ "571-403156-3" (หรือลงท้ายด้วย 4031563, 156-3, หรือ 403-156-3)
   - ชื่อบัญชีผู้รับคือ "นางสาว นภาพร นาควงศ์" (หรือ นภาพร น. / Naphaporn Nakwong)
2. ตรวจสอบ วัน เดือน ปี และเวลาที่โอน (สำคัญมาก เพื่อป้องกันสลิปปลอม/สลิปเก่า):
   - อ่าน วัน เดือน ปี (พ.ศ. หรือ ค.ศ.) และเวลาที่โอนบนสลิป
   - สลิปต้องเป็นสลิปปัจจุบันที่เพิ่งทำรายการในวันนี้ และเวลาโอนต้องใกล้เคียงกับเวลาปัจจุบัน ({current_time_str}) ย้อนหลังไม่เกิน 60 นาที หรือเกิดขึ้นหลังเวลาสรุปยอด ({booked_time_str})
   - หากวันที่โอนเป็นวันอื่นในอดีต (เช่น เมื่อวาน หลายวันก่อน หรือเดือนที่แล้ว) ถือว่า "ไม่ผ่าน" (is_recent = false)
3. สถานะการโอน: ต้องเป็นการทำรายการสำเร็จ (Success)
4. ยอดเงิน (Amount): ดึงตัวเลขยอดเงินที่โอนสำเร็จ
5. ชื่อผู้โอน (Sender Name)

กรุณาตอบเป็น JSON รูปแบบนี้เท่านั้น:
{{
  "is_valid_slip": true/false, // true เมื่อเป็นสลิปโอนเงินจริงที่โอนสำเร็จ
  "recipient_valid": true/false, // true เมื่อธนาคาร เลขบัญชี หรือชื่อผู้รับตรงกับ นภาพร นาควงศ์ ไทยพาณิชย์
  "transfer_datetime_str": "วันเวลาที่โอนตามที่ระบุบนสลิป",
  "is_recent": true/false, // true หากวันเดือนปีและเวลาที่โอนเป็นปัจจุบัน (ย้อนหลังไม่เกิน 60 นาที หรือเกิดขึ้นในรอบการสั่งซื้อนี้)
  "amount": ยอดเงินที่โอน (float หรือ int เช่น 200, 400, 600),
  "sender_name": "ชื่อผู้โอน",
  "reason": "คำอธิบายผลการตรวจสอบ โดยเฉพาะเรื่องวันเวลาที่โอนและบัญชีผู้รับ"
}}
"""
    try:
        pil_img = Image.open(io.BytesIO(image_bytes))
        res = vision_model.generate_content([prompt, pil_img])
        raw_text = res.text.strip()
        json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        print("Error verifying slip:", e)
    return {
        "is_valid_slip": False,
        "recipient_valid": False,
        "is_recent": False,
        "amount": 0,
        "sender_name": "",
        "reason": "ไม่สามารถอ่านสลิปได้"
    }

# ==============================================================================
# Webhook Endpoint
# ==============================================================================

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

    # [ขั้นตอนที่ 1]: ตรวจสอบคีย์เวิร์ด "ซื้อบัตร" หรือเจตนาสั่งซื้อบัตร
    if "ซื้อบัตร" in user_text or "จองบัตร" in user_text:
        user_sessions[user_id] = {
            "step": 1,
            "name": "",
            "phone": "",
            "tickets": 1,
            "total_price": TICKET_PRICE,
            "booked_at": datetime.now(THAI_TZ)
        }
        reply_text = get_step1_text()
        send_reply(event.reply_token, [TextMessage(text=reply_text)])
        return

    # [ขั้นตอนที่ 2]: หากลูกค้าอยู่ในขั้นตอนที่ 1 หรือส่งข้อมูลชื่อ/เบอร์โทรกลับมา
    extracted = extract_customer_info(user_text)
    if extracted.get("is_booking_info") or session.get("step") == 1:
        name = extracted.get("name") or session.get("name") or "คุณลูกค้า"
        phone = extracted.get("phone") or session.get("phone") or "เบอร์ติดต่อตามที่แจ้ง"
        tickets = int(extracted.get("tickets") or session.get("tickets") or 1)
        total_price = tickets * TICKET_PRICE

        # อัปเดตข้อมูล Session เข้าสู่ขั้นตอนที่ 2 พร้อมบันทึกเวลาออกยอดชำระ
        user_sessions[user_id] = {
            "step": 2,
            "name": name,
            "phone": phone,
            "tickets": tickets,
            "total_price": total_price,
            "booked_at": datetime.now(THAI_TZ)
        }

        reply_text = get_step2_text(name, phone, tickets, total_price)
        send_reply(event.reply_token, [TextMessage(text=reply_text)])
        return

    # กรณีถามคำถามทั่วไป (FAQ ทั่วไปของงาน Jabfest)
    try:
        response = text_model.generate_content(
            f"ลูกค้าถามว่า: '{user_text}'\nกรุณาตอบคำถามลูกค้าอย่างกระชับ สุภาพ เป็นกันเอง และหากลูกค้าสนใจซื้อบัตรให้แนะนำให้พิมพ์ 'ซื้อบัตร' ได้เลย"
        )
        reply_content = response.text.strip()
    except Exception:
        reply_content = "สอบถามข้อมูลเพิ่มเติมเรื่องบัตรหรือการเดินทาง ทักแอดมินหรือพิมพ์ 'ซื้อบัตร' ได้เลยนะคะ"

    send_reply(event.reply_token, [TextMessage(text=reply_content)])

# ------------------------------------------------------------------------------
# 2. จัดการรูปภาพสลิปโอนเงิน (Image Message - ขั้นตอนการตรวจสลิป)
# ------------------------------------------------------------------------------
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    user_id = event.source.user_id
    message_id = event.message.id
    session = user_sessions.get(user_id, {})
    booked_at = session.get("booked_at", datetime.now(THAI_TZ))

    # ดาวน์โหลดรูปภาพสลิปจาก LINE Server
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(message_id)

    # ส่งให้ Gemini Vision ตรวจสอบอย่างละเอียด
    slip_result = verify_slip_image(image_bytes, booked_at_dt=booked_at)
    
    is_valid_slip = slip_result.get("is_valid_slip", False)
    recipient_valid = slip_result.get("recipient_valid", False)
    is_recent = slip_result.get("is_recent", False)
    slip_amount = slip_result.get("amount", 0)

    # ดึงข้อมูลการจองของลูกค้า
    name = session.get("name", "คุณลูกค้า")
    phone = session.get("phone", "")
    tickets = session.get("tickets", 1)
    expected_amount = session.get("total_price", tickets * TICKET_PRICE)
    confirmed_amount = int(slip_amount) if slip_amount and slip_amount > 0 else expected_amount

    # ตรวจสอบ 3 เงื่อนไข: สลิปสมบูรณ์ + บัญชีผู้รับถูกต้อง + เวลาโอนเป็นปัจจุบัน
    if is_valid_slip and recipient_valid and is_recent:
        # [ยืนยันการจองสำเร็จ]: ส่งข้อความยืนยันการจอง + สติกเกอร์ขอบคุณ
        reply_text = get_step3_text(name, phone, tickets, confirmed_amount)
        
        # สติกเกอร์ขอบคุณของ LINE (Brown & Cony Thank You)
        sticker_msg = StickerMessage(package_id="446", sticker_id="1988")
        
        user_sessions[user_id]["step"] = 3
        send_reply(event.reply_token, [TextMessage(text=reply_text), sticker_msg])

    elif is_valid_slip and recipient_valid and not is_recent:
        # กรณีตรวจพบว่าโอนเข้าบัญชีถูก แต่สลิปเป็นวันเวลาเก่า (สลิปเก่า/นำมาใช้ซ้ำ)
        transfer_time = slip_result.get("transfer_datetime_str", "เวลาในอดีต")
        err_msg = (
            f"ขออภัยค่ะ แอดมินตรวจสอบพบว่า วันและเวลาที่โอนบนสลิป ({transfer_time}) "
            "ไม่สอดคล้องกับเวลาการทำรายการในปัจจุบันค่ะ\n\n"
            "เพื่อความปลอดภัยและการตรวจสอบที่ถูกต้อง รบกวนส่งสลิปการโอนเงินล่าสุดที่เพิ่งทำรายการในรอบนี้อีกครั้งนะคะ"
        )
        send_reply(event.reply_token, [TextMessage(text=err_msg)])

    else:
        # กรณีบัญชีผู้รับไม่ถูกต้อง หรือสลิปไม่สมบูรณ์
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