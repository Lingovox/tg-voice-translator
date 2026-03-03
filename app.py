import os
import time
import json
import logging
import tempfile
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

import requests
import jwt

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
    DateTime,
    func,
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import IntegrityError


# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")


# ============================================================
# ENV
# ============================================================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Models (can be overridden in env)
OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "whisper-1").strip()
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini").strip()
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "tts-1").strip()
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "alloy").strip()

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
CRYPTOCLOUD_SECRET_KEY = os.getenv("CRYPTOCLOUD_SECRET_KEY", "").strip()

TRIAL_LIMIT = int(os.getenv("TRIAL_LIMIT", "5"))
TRIAL_MAX_SECONDS = int(os.getenv("TRIAL_MAX_SECONDS", "60"))
MIN_BILLABLE_SECONDS = int(os.getenv("MIN_BILLABLE_SECONDS", "1"))


# ============================================================
# CONSTANTS
# ============================================================
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

POSTBACK_PATH = "/payments/cryptocloud/postback"
CC_CREATE_INVOICE_URL = "https://api.cryptocloud.plus/v1/invoice/create"

OPENAI_BASE = "https://api.openai.com/v1"

PACKAGES = {
    "P30":  {"usd": 3,  "minutes": 30},
    "P60":  {"usd": 8,  "minutes": 60},
    "P180": {"usd": 20, "minutes": 180},
    "P600": {"usd": 50, "minutes": 600},
}

LANGS = [
    ("English",    "en"),
    ("Russian",    "ru"),
    ("German",     "de"),
    ("Spanish",    "es"),
    ("Thai",       "th"),
    ("Vietnamese", "vi"),
    ("French",     "fr"),
    ("Turkish",    "tr"),
    ("Chinese",    "zh"),
    ("Arabic",     "ar"),
]


# ============================================================
# DB
# ============================================================
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True, index=True, nullable=False)
    target_lang = Column(String, nullable=False, default="en")

    trial_left = Column(Integer, nullable=False, default=TRIAL_LIMIT)
    balance_seconds = Column(Integer, nullable=False, default=0)

    is_subscribed = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)

    order_id = Column(String, nullable=False, unique=True, index=True)
    invoice_id = Column(String, nullable=False, unique=True, index=True)

    package_code = Column(String, nullable=False)
    amount_usd = Column(Integer, nullable=False)

    # created -> paid -> credited
    status = Column(String, nullable=False, default="created")

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)


# ============================================================
# TELEGRAM HELPERS
# ============================================================
def tg_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{TG_API}/{method}"
    r = requests.post(url, json=payload, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"ok": False, "raw": r.text, "status": r.status_code}
    if not data.get("ok", False):
        log.warning(f"Telegram API error in {method}: {data}")
    return data


def tg_send_message(chat_id: int, text_: str, reply_markup: Optional[Dict[str, Any]] = None):
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text_}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    tg_request("sendMessage", payload)


def tg_answer_callback(callback_query_id: str):
    tg_request("answerCallbackQuery", {"callback_query_id": callback_query_id})


def tg_get_file_path(file_id: str) -> str:
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"getFile failed: {data}")
    return data["result"]["file_path"]


def tg_download_file(file_path: str) -> bytes:
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def tg_send_voice(chat_id: int, voice_bytes: bytes, caption: Optional[str] = None):
    url = f"{TG_API}/sendVoice"
    files = {"voice": ("speech.ogg", voice_bytes)}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    r = requests.post(url, data=data, files=files, timeout=60)
    try:
        j = r.json()
    except Exception:
        j = {"ok": False, "raw": r.text, "status": r.status_code}
    if not j.get("ok", False):
        log.warning(f"sendVoice error: {j}")
    return j


def build_main_keyboard(selected_lang: str) -> Dict[str, Any]:
    rows = []
    for i in range(0, len(LANGS), 2):
        pair = LANGS[i:i + 2]
        row = []
        for title, code in pair:
            prefix = "✅ " if code == selected_lang else ""
            row.append({"text": f"{prefix}{title}", "callback_data": f"lang:{code}"})
        rows.append(row)

    rows.append([{"text": "💳 Buy minutes", "callback_data": "buy:menu"}])
    rows.append([{"text": "📌 Balance", "callback_data": "me:balance"}])
    rows.append([{"text": "💬 Support", "callback_data": "support"}])

    return {"inline_keyboard": rows}


def build_packages_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "30 min — $3", "callback_data": "buy:P30"}],
            [{"text": "60 min — $8", "callback_data": "buy:P60"}],
            [{"text": "180 min — $20", "callback_data": "buy:P180"}],
            [{"text": "600 min — $50", "callback_data": "buy:P600"}],
            [{"text": "⬅️ Back", "callback_data": "buy:back"}],
        ]
    }


def format_status_text(user: User) -> str:
    bal_min = max(0, int(user.balance_seconds or 0)) // 60
    return (
        "🎙 Voice Translator\n\n"
        f"🌍 Target language: {user.target_lang}\n"
        f"🎁 Free trials left: {int(user.trial_left or 0)} (≤ {TRIAL_MAX_SECONDS} sec)\n"
        f"💳 Balance: {bal_min} min\n\n"
        "Send a voice message — I will translate it and reply with voice."
    )


def send_menu(chat_id: int, user: User, extra_text: Optional[str] = None):
    text_ = extra_text if extra_text else format_status_text(user)
    tg_send_message(chat_id, text_, reply_markup=build_main_keyboard(user.target_lang))


# ============================================================
# CRYPTOCLOUD HELPERS
# ============================================================
def env_missing_for_payments() -> list:
    missing = []
    if not BASE_URL:
        missing.append("BASE_URL")
    if not CRYPTOCLOUD_API_KEY:
        missing.append("CRYPTOCLOUD_API_KEY")
    if not CRYPTOCLOUD_SHOP_ID:
        missing.append("CRYPTOCLOUD_SHOP_ID")
    if not CRYPTOCLOUD_SECRET_KEY:
        missing.append("CRYPTOCLOUD_SECRET_KEY")
    return missing


def cryptocloud_create_invoice(order_id: str, amount_usd: int, description: str) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Token {CRYPTOCLOUD_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "shop_id": CRYPTOCLOUD_SHOP_ID,
        "amount": amount_usd,
        "currency": "USD",
        "order_id": order_id,
        "comment": description,
        "success_url": f"{BASE_URL}/",
        "fail_url": f"{BASE_URL}/",
    }

    r = requests.post(CC_CREATE_INVOICE_URL, headers=headers, json=payload, timeout=30)
    ct = (r.headers.get("content-type") or "").lower()
    if "application/json" not in ct:
        return {"ok": False, "status": r.status_code, "raw": r.text}

    data = r.json()
    return {"ok": r.status_code == 200, "status": r.status_code, "data": data}


def verify_postback_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, CRYPTOCLOUD_SECRET_KEY, algorithms=["HS256"])
    except Exception as e:
        log.warning(f"JWT verify failed: {e}")
        return None


# ============================================================
# OPENAI HELPERS (REAL VOICE)
# ============================================================
def openai_headers() -> Dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return {"Authorization": f"Bearer {OPENAI_API_KEY}"}


def ffmpeg_ogg_to_wav(ogg_path: str, wav_path: str):
    cmd = ["ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path]
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found in container. Install ffmpeg in Dockerfile.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg convert failed: {e.stderr.decode('utf-8', errors='ignore')}")


def openai_transcribe_wav(wav_path: str) -> str:
    url = f"{OPENAI_BASE}/audio/transcriptions"
    headers = openai_headers()
    data = {"model": OPENAI_STT_MODEL}
    with open(wav_path, "rb") as f:
        files = {"file": ("audio.wav", f, "audio/wav")}
        r = requests.post(url, headers=headers, data=data, files=files, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"STT failed HTTP {r.status_code}: {r.text}")
    j = r.json()
    return (j.get("text") or "").strip()


def openai_translate_text(text: str, target_lang: str) -> str:
    url = f"{OPENAI_BASE}/chat/completions"
    headers = openai_headers()
    headers["Content-Type"] = "application/json"

    system = (
        "You are a professional translator. "
        "Translate the user text accurately and naturally. "
        "Return only the translation without quotes."
    )
    user = f"Target language: {target_lang}\n\nText:\n{text}"

    payload = {
        "model": OPENAI_TEXT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }

    r = requests.post(url, headers=headers, json=payload, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Translate failed HTTP {r.status_code}: {r.text}")

    j = r.json()
    out = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return (out or "").strip()


def openai_tts_to_ogg(text: str) -> bytes:
    url = f"{OPENAI_BASE}/audio/speech"
    headers = openai_headers()
    headers["Content-Type"] = "application/json"

    payload = {
        "model": OPENAI_TTS_MODEL,
        "voice": OPENAI_TTS_VOICE,
        "input": text,
        "format": "opus",
    }

    r = requests.post(url, headers=headers, json=payload, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"TTS failed HTTP {r.status_code}: {r.text}")

    return r.content


# ============================================================
# BUSINESS LOGIC: USERS + BILLING
# ============================================================
def get_or_create_user(db, chat_id: int) -> User:
    user = db.get(User, int(chat_id))
    if user:
        if user.target_lang is None:
            user.target_lang = "en"
        if user.trial_left is None:
            user.trial_left = TRIAL_LIMIT
        if user.balance_seconds is None:
            user.balance_seconds = 0
        return user

    user = User(
        telegram_id=int(chat_id),
        target_lang="en",
        trial_left=TRIAL_LIMIT,
        balance_seconds=0,
        is_subscribed=False,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def seconds_to_bill(duration: int) -> int:
    d = int(duration or 0)
    if d <= 0:
        return 0
    return max(MIN_BILLABLE_SECONDS, d)


def can_use_trial(user: User, duration: int) -> bool:
    return (int(user.trial_left or 0) > 0) and (int(duration) <= TRIAL_MAX_SECONDS)


def check_can_process_voice(user: User, duration: int) -> Tuple[bool, str, Dict[str, Any]]:
    duration = int(duration or 0)
    bill = seconds_to_bill(duration)
    if bill <= 0:
        return False, "I can't detect the audio duration. Please try again.", {}

    if can_use_trial(user, duration):
        return True, "", {"mode": "trial", "billed_seconds": bill}

    bal = int(user.balance_seconds or 0)
    if bal < bill:
        need_min = (bill - bal + 59) // 60
        return False, f"❌ Not enough minutes. You need about {need_min} more min.\nTap “💳 Buy minutes”.", {}

    return True, "", {"mode": "paid", "billed_seconds": bill}


def commit_debit_after_success(db, user: User, duration: int, ctx: Dict[str, Any]) -> None:
    duration = int(duration or 0)
    bill = int(ctx.get("billed_seconds") or seconds_to_bill(duration))
    mode = ctx.get("mode")

    if bill <= 0:
        return

    if mode == "trial":
        user.trial_left = max(0, int(user.trial_left or 0) - 1)
        user.updated_at = datetime.utcnow()
        db.add(user)
        db.commit()
        return

    bal = int(user.balance_seconds or 0)
    new_bal = bal - bill
    if new_bal < 0:
        new_bal = 0
    user.balance_seconds = new_bal
    user.updated_at = datetime.utcnow()
    db.add(user)
    db.commit()


# ============================================================
# ADMIN
# ============================================================
def is_admin(chat_id: int) -> bool:
    return bool(ADMIN_ID) and str(chat_id) == str(ADMIN_ID)


def build_stats_text(db) -> str:
    users_total = db.query(func.count(User.telegram_id)).scalar() or 0
    users_with_balance = db.query(func.count(User.telegram_id)).filter(User.balance_seconds > 0).scalar() or 0

    created_cnt = db.query(func.count(Payment.id)).filter(Payment.status == "created").scalar() or 0
    paid_cnt = db.query(func.count(Payment.id)).filter(Payment.status == "paid").scalar() or 0
    credited_cnt = db.query(func.count(Payment.id)).filter(Payment.status == "credited").scalar() or 0

    revenue_usd = db.query(func.coalesce(func.sum(Payment.amount_usd), 0)).filter(Payment.status == "credited").scalar() or 0

    return (
        "📊 Admin stats\n\n"
        f"👤 Users total: {users_total}\n"
        f"💳 Users with balance: {users_with_balance}\n\n"
        f"🧾 Payments created: {created_cnt}\n"
        f"✅ Payments paid: {paid_cnt}\n"
        f"🏁 Payments credited: {credited_cnt}\n\n"
        f"💰 Revenue (credited): ${revenue_usd}\n"
    )


# ============================================================
# REAL VOICE PIPELINE
# ============================================================
def process_voice_real(file_id: str, target_lang: str) -> Tuple[str, bytes]:
    file_path = tg_get_file_path(file_id)
    ogg_bytes = tg_download_file(file_path)

    with tempfile.TemporaryDirectory() as td:
        ogg_path = os.path.join(td, "voice.ogg")
        wav_path = os.path.join(td, "voice.wav")

        with open(ogg_path, "wb") as f:
            f.write(ogg_bytes)

        ffmpeg_ogg_to_wav(ogg_path, wav_path)

        transcript = openai_transcribe_wav(wav_path)
        if not transcript:
            raise RuntimeError("Empty transcript")

        translated = openai_translate_text(transcript, target_lang)
        tts_bytes = openai_tts_to_ogg(translated)

        return translated, tts_bytes


# ============================================================
# APP
# ============================================================
app = FastAPI()


@app.on_event("startup")
def startup():
    init_db()
    log.info("Startup complete")


@app.get("/")
def root():
    return {"ok": True, "service": "tg-voice-translator"}


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================
@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()

    try:
        # ---------------- MESSAGE ----------------
        if "message" in update:
            msg = update["message"]
            chat_id = (msg.get("chat") or {}).get("id")
            text_ = (msg.get("text") or "").strip()

            if not chat_id:
                return JSONResponse({"ok": True})

            with SessionLocal() as db:
                user = get_or_create_user(db, int(chat_id))

                # --- Support ticket: user replies to our Support prompt ---
                if "reply_to_message" in msg:
                    rt = msg.get("reply_to_message") or {}
                    rt_text = (rt.get("text") or "").strip()
                    if rt_text.startswith("💬 Support request"):
                        if not ADMIN_ID:
                            tg_send_message(chat_id, "Support is not configured. Please try later.")
                            return JSONResponse({"ok": True})

                        user_from = msg.get("from") or {}
                        username = user_from.get("username")
                        first_name = user_from.get("first_name", "")
                        last_name = user_from.get("last_name", "")
                        display_name = (first_name + " " + last_name).strip() or "User"
                        user_tag = f"@{username}" if username else "(no username)"

                        body = (msg.get("text") or "").strip()
                        if not body:
                            tg_send_message(chat_id, "Please describe your issue as text (one message).")
                            return JSONResponse({"ok": True})

                        # Send to admin
                        tg_send_message(
                            int(ADMIN_ID),
                            "🆘 Support ticket\n"
                            f"From: {display_name} {user_tag}\n"
                            f"telegram_id: {chat_id}\n\n"
                            f"Message:\n{body}"
                        )
                        # Forward the original message too (keeps Telegram context)
                        try:
                            tg_request("forwardMessage", {
                                "chat_id": int(ADMIN_ID),
                                "from_chat_id": int(chat_id),
                                "message_id": int(msg["message_id"]),
                            })
                        except Exception:
                            pass

                        tg_send_message(chat_id, "✅ Got it! Your message was sent to support.")
                        return JSONResponse({"ok": True})

                if text_ == "/support":
                    if not ADMIN_ID:
                        tg_send_message(chat_id, "Support is not configured. Please try later.")
                        return JSONResponse({"ok": True})

                    tg_send_message(
                        chat_id,
                        "💬 Support request\n\n"
                        "Please reply to THIS message with:\n"
                        "- what happened\n"
                        "- (optional) Order ID / Invoice ID\n\n"
                        "I will receive it and respond.",
                        reply_markup={"force_reply": True, "input_field_placeholder": "Describe your issue…"},
                    )
                    return JSONResponse({"ok": True})


                if text_ in ("/start", "/menu"):
                    send_menu(chat_id, user)
                    return JSONResponse({"ok": True})

                if text_ == "/buy":
                    tg_send_message(chat_id, "💳 Choose a minutes package:", reply_markup=build_packages_keyboard())
                    return JSONResponse({"ok": True})

                if text_ == "/me":
                    send_menu(chat_id, user)
                    return JSONResponse({"ok": True})

                # Admin stats: support /stats and /stat
                if text_ in ("/stats", "/stat"):
                    if not is_admin(chat_id):
                        tg_send_message(chat_id, "⛔️ Access denied.")
                        return JSONResponse({"ok": True})
                    tg_send_message(chat_id, build_stats_text(db))
                    return JSONResponse({"ok": True})

                # ---------------- VOICE ----------------
                if "voice" in msg:
                    voice = msg["voice"] or {}
                    duration = int(voice.get("duration") or 0)
                    file_id = voice.get("file_id")

                    if not file_id:
                        send_menu(chat_id, user, extra_text="I can't get the file_id. Please try again.")
                        return JSONResponse({"ok": True})

                    ok, err_text, ctx = check_can_process_voice(user, duration)
                    if not ok:
                        send_menu(chat_id, user, extra_text=err_text)
                        return JSONResponse({"ok": True})

                    try:
                        translated_text, tts_ogg = process_voice_real(file_id, user.target_lang)
                    except Exception as e:
                        log.exception(f"voice processing failed: {e}")
                        hint = ""
                        if "ffmpeg not found" in str(e).lower():
                            hint = "\n\n⚠️ ffmpeg is missing in the container. Install it in Dockerfile."
                        send_menu(chat_id, user, extra_text=f"❌ Processing error: {e}{hint}")
                        return JSONResponse({"ok": True})

                    commit_debit_after_success(db, user, duration, ctx)
                    db.refresh(user)

                    tg_send_voice(
                        chat_id,
                        tts_ogg,
                        caption=f"✅ Translation: {translated_text[:900]}",
                    )

                    send_menu(chat_id, user)
                    return JSONResponse({"ok": True})

                # default: keep menu visible
                send_menu(chat_id, user)
                return JSONResponse({"ok": True})

        # ------------- CALLBACK QUERY -------------
        if "callback_query" in update:
            cq = update["callback_query"]
            cb_id = cq.get("id")
            data = (cq.get("data") or "").strip()
            chat_id = (((cq.get("message") or {}).get("chat") or {}).get("id"))

            if cb_id:
                tg_answer_callback(cb_id)

            if not chat_id:
                return JSONResponse({"ok": True})

            with SessionLocal() as db:
                user = get_or_create_user(db, int(chat_id))

                if data == "support":
                    if not ADMIN_ID:
                        tg_send_message(chat_id, "Support is not configured. Please try later.")
                        return JSONResponse({"ok": True})

                    tg_send_message(
                        chat_id,
                        "💬 Support request\n\n"
                        "Please reply to THIS message with:\n"
                        "- what happened\n"
                        "- (optional) Order ID / Invoice ID\n\n"
                        "I will receive it and respond.",
                        reply_markup={"force_reply": True, "input_field_placeholder": "Describe your issue…"},
                    )
                    return JSONResponse({"ok": True})


                if data.startswith("lang:"):
                    lang = data.split(":", 1)[1].strip()
                    allowed = {c for _, c in LANGS}
                    if lang in allowed:
                        user.target_lang = lang
                        user.updated_at = datetime.utcnow()
                        db.add(user)
                        db.commit()
                        db.refresh(user)
                    send_menu(chat_id, user)
                    return JSONResponse({"ok": True})

                if data == "me:balance":
                    send_menu(chat_id, user)
                    return JSONResponse({"ok": True})

                if data == "buy:menu":
                    tg_send_message(chat_id, "💳 Choose a minutes package:", reply_markup=build_packages_keyboard())
                    return JSONResponse({"ok": True})

                if data == "buy:back":
                    send_menu(chat_id, user)
                    return JSONResponse({"ok": True})

                if data.startswith("buy:"):
                    package_code = data.split(":", 1)[1].strip().upper()
                    if package_code not in PACKAGES:
                        send_menu(chat_id, user, extra_text="Unknown package.")
                        return JSONResponse({"ok": True})

                    missing = env_missing_for_payments()
                    if missing:
                        send_menu(chat_id, user, extra_text=f"Error: missing env vars: {', '.join(missing)}")
                        return JSONResponse({"ok": True})

                    amount_usd = int(PACKAGES[package_code]["usd"])
                    order_id = f"{chat_id}_{package_code}_{int(time.time())}"
                    description = f"Minutes package {package_code} for user {chat_id}"

                    cc = cryptocloud_create_invoice(order_id, amount_usd, description)
                    if not cc["ok"]:
                        send_menu(
                            chat_id,
                            user,
                            extra_text=(
                                f"Invoice creation failed: {CC_CREATE_INVOICE_URL} -> HTTP {cc.get('status')}\n"
                                f"{cc.get('raw') or cc.get('data')}"
                            ),
                        )
                        return JSONResponse({"ok": True})

                    data_json = cc["data"]
                    result = data_json.get("result") or data_json.get("data") or data_json
                    invoice_uuid = result.get("uuid") or result.get("invoice_id") or result.get("id")
                    pay_url = result.get("link") or result.get("pay_url") or result.get("url")

                    if not invoice_uuid:
                        send_menu(chat_id, user, extra_text=f"CryptoCloud response has no invoice uuid: {data_json}")
                        return JSONResponse({"ok": True})

                    try:
                        p = Payment(
                            telegram_id=int(chat_id),
                            order_id=order_id,
                            invoice_id=str(invoice_uuid),
                            package_code=package_code,
                            amount_usd=amount_usd,
                            status="created",
                            created_at=datetime.utcnow(),
                            updated_at=datetime.utcnow(),
                        )
                        db.add(p)
                        db.commit()
                    except IntegrityError as e:
                        db.rollback()
                        log.warning(f"Payment insert IntegrityError: {e}")

                    if pay_url:
                        kb = {"inline_keyboard": [[{"text": "Go to payment ✅", "url": pay_url}]]}
                        tg_send_message(
                            chat_id,
                            f"Invoice created.\nPackage: {package_code}\nAmount: ${amount_usd}\n\nTap the button below:",
                            reply_markup=kb,
                        )
                    else:
                        tg_send_message(chat_id, f"Invoice created: {invoice_uuid}\n(No payment link in response)")

                    return JSONResponse({"ok": True})

                send_menu(chat_id, user)
                return JSONResponse({"ok": True})

        return JSONResponse({"ok": True})

    except Exception:
        log.exception("telegram_webhook error")
        return JSONResponse({"ok": True})


# ============================================================
# CRYPTOCLOUD POSTBACK
# ============================================================
@app.post(POSTBACK_PATH)
async def cryptocloud_postback(req: Request):
    raw = await req.body()

    try:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = json.loads(raw)

        log.info("==== RAW POSTBACK ====")
        log.info(payload)

        status = (payload.get("status") or "").lower().strip()
        order_id = payload.get("order_id")
        token = payload.get("token")

        if not token:
            return PlainTextResponse("no token", status_code=400)

        decoded = verify_postback_token(token)
        if not decoded:
            return PlainTextResponse("bad token", status_code=400)

        token_invoice_id = decoded.get("id")
        postback_invoice_id = payload.get("invoice_id")
        invoice_info = payload.get("invoice_info") or {}
        invoice_uuid = invoice_info.get("uuid")

        effective_invoice_id = invoice_uuid or postback_invoice_id or token_invoice_id
        if not effective_invoice_id and not order_id:
            return PlainTextResponse("no invoice_id/order_id", status_code=400)

        is_paid = status in ("success", "paid")
        invoice_status = (invoice_info.get("invoice_status") or "").lower().strip()
        if invoice_status in ("success", "paid"):
            is_paid = True

        with SessionLocal() as db:
            pay = None
            if order_id:
                pay = db.query(Payment).filter(Payment.order_id == order_id).first()
            if not pay and effective_invoice_id:
                pay = db.query(Payment).filter(Payment.invoice_id == str(effective_invoice_id)).first()

            if not pay:
                log.warning(f"Payment not found for order_id={order_id} invoice_id={effective_invoice_id}")
                return PlainTextResponse("payment not found", status_code=200)

            current_status = (pay.status or "").lower().strip()

            if current_status == "credited":
                log.info("Already credited, skip")
                return PlainTextResponse("ok", status_code=200)

            if not is_paid:
                pay.status = status or "unknown"
                pay.updated_at = datetime.utcnow()
                db.add(pay)
                db.commit()
                log.info(f"Status not paid: {pay.status}")
                return PlainTextResponse("ok", status_code=200)

            pkg_code = (pay.package_code or "").strip().upper()
            pkg = PACKAGES.get(pkg_code)

            pay.status = "paid"
            pay.updated_at = datetime.utcnow()
            db.add(pay)
            db.commit()

            if not pkg:
                log.warning(f"Unknown package_code in DB: {pay.package_code}")
                return PlainTextResponse("ok", status_code=200)

            add_seconds = int(pkg["minutes"] * 60)

            user = db.get(User, int(pay.telegram_id))
            if not user:
                user = User(
                    telegram_id=int(pay.telegram_id),
                    target_lang="en",
                    trial_left=TRIAL_LIMIT,
                    balance_seconds=0,
                    is_subscribed=False,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                db.add(user)
                db.commit()
                db.refresh(user)

            before = int(user.balance_seconds or 0)
            user.balance_seconds = before + add_seconds
            user.updated_at = datetime.utcnow()

            pay.status = "credited"
            pay.updated_at = datetime.utcnow()

            db.add(user)
            db.add(pay)
            db.commit()
            db.refresh(user)

            after = int(user.balance_seconds or 0)
            log.info(
                f"CREDITED: tg_id={user.telegram_id} package={pkg_code} +{add_seconds}s "
                f"({before}s -> {after}s) order={pay.order_id}"
            )

            tg_send_message(
                int(user.telegram_id),
                f"✅ Payment received!\nPackage: {pkg_code}\nCredited: {pkg['minutes']} min\nBalance: {after // 60} min",
                reply_markup=build_main_keyboard(user.target_lang),
            )

        return PlainTextResponse("ok", status_code=200)

    except Exception:
        log.exception("postback error")
        return PlainTextResponse("error", status_code=200)
