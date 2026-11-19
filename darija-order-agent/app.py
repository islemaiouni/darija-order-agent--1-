import os
import re
import time
import logging
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, FileResponse, JSONResponse
from pydantic import BaseModel, Field
from twilio.twiml.voice_response import VoiceResponse
from twilio.request_validator import RequestValidator

load_dotenv()
from db import (init_db, get_order, get_call, get_latest_call, get_turns, add_turn,
                create_order, create_call, finalize_order, set_call_status,
                recent_order_for_phone)
from services import synthesize_darija, transcribe_algerian_arabic, understand_customer, cleanup_audio
from telephony import start_call

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

app = FastAPI(title="Algerian Darija Order Confirmation Agent")
ROOT = Path(__file__).parent
AUDIO_DIR = ROOT / "audio"
STATIC_DIR = ROOT / "static"
MAX_TURNS = int(os.getenv("MAX_TURNS", "5"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "5"))

# Demo catalogue. Prices live on the server so the browser can't change them.
PRODUCTS = [
    {"id": "a55", "name": "Samsung Galaxy A55", "price_dzd": 45000},
    {"id": "airpods", "name": "Écouteurs Bluetooth", "price_dzd": 6500},
    {"id": "watch", "name": "Montre connectée", "price_dzd": 9800},
    {"id": "charger", "name": "Chargeur rapide 65W", "price_dzd": 3200},
]

# Fixed phrases (cached TTS)
MSG_NOT_FOUND = "سمحلي، ما قدرناش نلقاو الطلبية. راح يتواصل معاك شخص من الفريق."
MSG_ERROR = "صار خطأ تقني. راح يتواصل معاك الفريق."
MSG_NO_HEAR = "سمحلي، ما سمعتكش مليح. تقدر تعاود تجاوبني بنعم أو لا؟"
MSG_CONFIRMED = "شكراً ليك. أكدنا الطلبية تاعك. نهارك مبروك."
MSG_REJECTED = "فهمتك. سجلنا بلي ما حبيتش تأكد الطلبية. شكراً على وقتك."
MSG_CHANGE = "فهمت طلب التغيير. ما أكدناش الطلبية، وخليت الفريق يتواصل معاك باش يراجع التعديل. شكراً."
MSG_GIVE_UP = "سمحلي ما قدرناش نأكدوا الإجابة. نخليو واحد من الفريق يتواصل معاك."
MSG_REPROMPT = "باش نكون متأكد، تحب نأكدوا الطلبية؟ جاوب بنعم ولا لا."
MSG_ASK_AGAIN = "واش تأكد الطلبية؟ جاوب بنعم ولا لا."


@app.on_event("startup")
def startup():
    init_db()
    cleanup_audio()


# ------------------------------------------------------------------ helpers
def public_url(path: str) -> str:
    return os.environ["PUBLIC_BASE_URL"].rstrip("/") + path


async def verify_twilio(request: Request, form_data: dict):
    if os.getenv("SKIP_TWILIO_SIGNATURE_VALIDATION", "false").lower() == "true":
        return
    # Rebuild the exact public URL Twilio signed (we may be behind ngrok / a proxy).
    url = public_url(request.url.path)
    if request.url.query:
        url += "?" + request.url.query
    signature = request.headers.get("X-Twilio-Signature", "")
    validator = RequestValidator(os.getenv("TWILIO_AUTH_TOKEN", ""))
    if not validator.validate(url, form_data, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


def twiml_response(vr: VoiceResponse):
    return Response(content=str(vr), media_type="application/xml")


async def speak(vr: VoiceResponse, text: str, cache: bool = False):
    """Synthesize Darija speech and add it to the TwiML as <Play>."""
    filename = await run_in_threadpool(synthesize_darija, text, cache)
    vr.play(public_url(f"/audio/{filename}"))


def record_next(vr: VoiceResponse, order_id: str):
    next_url = public_url(f"/process?order_id={order_id}")
    vr.record(action=next_url, method="POST", max_length=12, timeout=5,
              play_beep=False, trim="trim-silence")
    # If the customer says nothing, Twilio falls through to here -> counts as an empty turn.
    vr.redirect(next_url, method="POST")


def normalize_phone(raw: str) -> str:
    p = re.sub(r"[\s\-().]", "", raw or "")
    if p.startswith("00"):
        p = "+" + p[2:]
    if p.startswith("0") and len(p) == 10:          # 05xx.. / 06xx.. / 07xx..
        p = "+213" + p[1:]
    elif re.fullmatch(r"[567]\d{8}", p):             # 5xxxxxxxx
        p = "+213" + p
    elif p.startswith("213"):
        p = "+" + p
    if not re.fullmatch(r"\+\d{8,15}", p):
        raise ValueError("Invalid phone number")
    return p


def allowed_phones() -> set:
    raw = os.getenv("ALLOWED_PHONES", "")
    out = set()
    for item in raw.split(","):
        if item.strip():
            out.add(normalize_phone(item.strip()))
    return out


# ------------------------------------------------------------------ website + API
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"ok": True, "service": "darija-order-agent"}


@app.get("/api/products")
def products():
    return PRODUCTS


class OrderIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    phone: str = Field(min_length=8, max_length=25)
    product_id: str
    quantity: int = Field(default=1, ge=1, le=5)
    address: str = Field(default="", max_length=160)
    consent: bool = False


@app.post("/api/orders")
def create_order_and_call(body: OrderIn):
    if not body.consent:
        raise HTTPException(400, "Please accept that an automated agent will call you.")
    try:
        phone = normalize_phone(body.phone)
    except ValueError:
        raise HTTPException(400, "Invalid phone number. Example: 0550123456 or +213550123456")
    allow = allowed_phones()
    if allow and phone not in allow:
        raise HTTPException(403, "Demo mode: this number is not in ALLOWED_PHONES.")
    product = next((p for p in PRODUCTS if p["id"] == body.product_id), None)
    if not product:
        raise HTTPException(400, "Unknown product")
    if recent_order_for_phone(phone, COOLDOWN_MINUTES):
        raise HTTPException(429, f"This number already has a recent order. Wait {COOLDOWN_MINUTES} minutes.")

    address = body.address.strip() or "Annaba, Algeria"
    total = product["price_dzd"] * body.quantity
    order_id = create_order(body.name.strip(), phone, product["name"], body.quantity, total, address)
    try:
        start_call(order_id)
    except Exception as e:  # noqa: BLE001
        log.exception("Could not start call")
        finalize_order("", order_id, "CALL_FAILED")
        raise HTTPException(502, f"Could not place the call: {e}")
    return {"order_id": order_id, "status": "PENDING_CONFIRMATION"}


@app.get("/api/orders/{order_id}")
def order_status(order_id: str):
    order = get_order(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    call = get_latest_call(order_id)
    return {
        "order_id": order["id"],
        "status": order["status"],
        "product": order["product_name"],
        "quantity": order["quantity"],
        "total_dzd": order["total_dzd"],
        "confirmed_at": order["confirmed_at"],
        "call_status": call["call_status"] if call else None,
        "turns": get_turns(order_id),
    }


@app.get("/audio/{filename}")
def get_audio(filename: str):
    if Path(filename).name != filename or not filename.endswith(".mp3"):
        raise HTTPException(404, "Audio not found")
    path = AUDIO_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Audio not found")
    return FileResponse(path, media_type="audio/mpeg")


# ------------------------------------------------------------------ Twilio webhooks
@app.post("/voice")
async def voice(request: Request):
    form = await request.form()
    await verify_twilio(request, dict(form))
    order_id = request.query_params.get("order_id", "")
    call_sid = str(form.get("CallSid", ""))
    order = get_order(order_id)
    vr = VoiceResponse()
    try:
        if not order or order["status"] != "PENDING_CONFIRMATION":
            await speak(vr, MSG_NOT_FOUND, cache=True)
            vr.hangup()
            return twiml_response(vr)

        create_call(call_sid, order_id)  # idempotent; avoids a race with start_call()
        greeting = (
            f"السلام عليكم {order['customer_name']}. راني مساعد آلي من المتجر، نعيطلك باش نأكدوا الطلبية تاعك. "
            f"طلبت {order['quantity']} من {order['product_name']}، والمبلغ الإجمالي "
            f"{order['total_dzd']} دينار. العنوان هو {order['delivery_address']}. "
            "واش تأكد الطلبية؟ جاوبني بنعم ولا لا."
        )
        await speak(vr, greeting)
        record_next(vr, order_id)
    except Exception:  # noqa: BLE001
        log.exception("/voice failed")
        vr = VoiceResponse()
        vr.hangup()
    return twiml_response(vr)


def analyze_recording(order: dict, recording_url: str, duration: int):
    """Blocking pipeline: download WAV -> Deepgram -> OpenAI. Runs in a worker thread."""
    transcript = ""
    if recording_url and duration >= 1:
        try:
            wav = None
            for attempt in range(3):
                r = requests.get(
                    recording_url + ".wav",
                    auth=(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]),
                    timeout=30,
                )
                if r.status_code == 200:
                    wav = r.content
                    break
                time.sleep(1)
            if wav:
                transcript = transcribe_algerian_arabic(wav)
        except Exception:  # noqa: BLE001
            log.exception("Recording/STT failed")
    if not transcript:
        return "", {"intent": "UNCLEAR", "confidence": 0.0, "response": MSG_NO_HEAR,
                    "requested_change": None}
    return transcript, understand_customer(order, transcript)


@app.post("/process")
async def process_recording(request: Request):
    form = await request.form()
    await verify_twilio(request, dict(form))
    call_sid = str(form.get("CallSid", ""))
    recording_url = str(form.get("RecordingUrl", ""))
    try:
        duration = int(float(form.get("RecordingDuration", "0") or 0))
    except ValueError:
        duration = 0
    order_id = request.query_params.get("order_id", "")
    session = get_call(call_sid)
    order = get_order(order_id)
    vr = VoiceResponse()

    try:
        if not session or not order or session["order_id"] != order_id:
            await speak(vr, MSG_ERROR, cache=True)
            vr.hangup()
            return twiml_response(vr)
        if order["status"] != "PENDING_CONFIRMATION":   # already decided (e.g. duplicate webhook)
            vr.hangup()
            return twiml_response(vr)

        transcript, result = await run_in_threadpool(analyze_recording, order, recording_url, duration)
        intent, conf = result["intent"], result["confidence"]
        turn = add_turn(call_sid, transcript, intent, result["response"])

        # Conservative: only explicit, high-confidence answers change the order.
        if intent == "CONFIRM" and conf >= 0.75:
            finalize_order(call_sid, order_id, "CONFIRMED")
            await speak(vr, MSG_CONFIRMED, cache=True)
            vr.hangup()
        elif intent == "REJECT" and conf >= 0.75:
            finalize_order(call_sid, order_id, "REJECTED")
            await speak(vr, MSG_REJECTED, cache=True)
            vr.hangup()
        elif intent == "CHANGE":
            finalize_order(call_sid, order_id, "NEEDS_HUMAN_REVIEW")
            await speak(vr, MSG_CHANGE, cache=True)
            vr.hangup()
        elif turn >= MAX_TURNS:
            finalize_order(call_sid, order_id, "NEEDS_HUMAN_REVIEW")
            await speak(vr, MSG_GIVE_UP, cache=True)
            vr.hangup()
        else:
            if intent in ("CONFIRM", "REJECT"):          # low confidence -> never trust LLM's wording
                await speak(vr, MSG_REPROMPT, cache=True)
            elif intent == "QUESTION":
                await speak(vr, result["response"])
                await speak(vr, MSG_ASK_AGAIN, cache=True)
            else:                                        # UNCLEAR
                await speak(vr, result["response"])
            record_next(vr, order_id)
    except Exception:  # noqa: BLE001
        log.exception("/process failed")
        try:
            finalize_order(call_sid, order_id, "NEEDS_HUMAN_REVIEW")
        except Exception:  # noqa: BLE001
            pass
        vr = VoiceResponse()
        vr.hangup()
    return twiml_response(vr)


@app.post("/call-status")
async def call_status(request: Request):
    form = await request.form()
    await verify_twilio(request, dict(form))
    call_sid = str(form.get("CallSid", ""))
    order_id = request.query_params.get("order_id", "")
    status = str(form.get("CallStatus", "unknown")).lower()
    if call_sid and order_id and get_order(order_id):
        create_call(call_sid, order_id)
        if status in {"no-answer", "busy", "failed", "canceled"}:
            finalize_order(call_sid, order_id, "NO_ANSWER")
        elif status == "completed":
            # Customer hung up before deciding -> a human should follow up.
            finalize_order(call_sid, order_id, "NEEDS_HUMAN_REVIEW")
        else:
            set_call_status(call_sid, status.upper())
    return JSONResponse({"ok": True})
