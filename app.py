import os
import time
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

import requests
import jwt

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean, DateTime
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import IntegrityError


# ============================
# Logging
# ============================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")


# ============================
# Env
# ============================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
CRYPTOCLOUD_SECRET_KEY = os.getenv("CRYPTOCLOUD_SECRET_KEY", "").strip()

TRIAL_LIMIT = int(os.getenv("TRIAL_LIMIT", "5"))
TRIAL_MAX_SECONDS = int(os.getenv("TRIAL_MAX_SECONDS", "60"))  # free message max duration
MIN_BILLABLE_SECONDS = int(os.getenv("MIN_BILLABLE_SECONDS", "1"))  # safety


# ============================
# Constants
# ============================
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# CryptoCloud v1 endpoints (IMPORTANT: without /api/v1)
CC_CREATE_INVOICE_URL = "https://api.cryptocloud.plus/v1/invoice/create"

POSTBACK_PATH = "/payments/cryptocloud/postback"

# Packages: code -> {usd, minutes}
PACKAGES = {
    "P30":  {"usd": 3,  "minutes": 30},
    "P60":  {"usd": 8,  "minutes": 60},
    "P180": {"usd": 20, "minutes": 180},
    "P600": {"usd": 50, "minutes": 600},
}

LANGS = [
    ("English", "en"),
    ("Русский", "ru"),
    ("Deutsch", "de"),
    ("Español", "es"),
    ("ไทย", "th"),
    ("Tiếng Việt", "vi"),
    ("Français", "fr"),
    ("Türkçe", "tr"),
    ("中文", "zh"),
    ("العربية", "ar"),
]


# ============================
# DB
# ============================
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True, index=True, nullable=False)
    target_lang = Column(String, nullable=False, default="en")

    # how many free messages left
    trial_left = Column(Integer, nullable=False, default=TRIAL_LIMIT)

    # optional counter (used by your schema)
    trial_messages = Column(Integer, nullable=False, default=0)

    # paid balance in seconds
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
    status = Column(String, nullable=False, default="created")  # created / paid / success / failed ...

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    message = Column(String, nullable=False)
    status = Column(String, nullable=False, default="open")  # open/closed
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)


# ============================
# Telegram helpers
# ============================
def tg_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{TG_API}/{method}"
    r = requests.post(url, json=payload, timeout=30)
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "raw": r.text, "status": r.status_code}
    return data


def tg_send_message(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_request("sendMessage", payload)


def tg_send_voice(chat_id: int, voice_bytes: bytes, caption: Optional[str] = None):
    url = f"{TG_API}/sendVoice"
    files = {"voice": ("voice.ogg", voice_bytes, "audio/ogg")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    r = requests.post(url, data=data, files=files, timeout=60)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "raw": r.text, "status": r.status_code}


def tg_answer_callback(callback_query_id: str, text: Optional[str] = None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return tg_request("answerCallbackQuery", payload)


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
    rows.append([{"text": "🆘 Support", "callback_data": "support:menu"}])
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
        "🎙 Lingovox — AI voice translator\n\n"
        f"🌍 Target language: {user.target_lang}\n"
        f"🎁 Free messages left: {user.trial_left} (≤ {TRIAL_MAX_SECONDS}s)\n"
        f"💳 Balance: {bal_min} min\n\n"
        "Send a voice message — I'll translate and reply with voice."
    )


# ============================
# CryptoCloud helpers
# ============================
def env_missing() -> list:
    missing = []
    if not CRYPTOCLOUD_API_KEY:
        missing.append("CRYPTOCLOUD_API_KEY")
    if not CRYPTOCLOUD_SHOP_ID:
        missing.append("CRYPTOCLOUD_SHOP_ID")
    if not CRYPTOCLOUD_SECRET_KEY:
        missing.append("CRYPTOCLOUD_SECRET_KEY")
    if not BASE_URL:
        missing.append("BASE_URL")
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
        decoded = jwt.decode(token, CRYPTOCLOUD_SECRET_KEY, algorithms=["HS256"])
        return decoded
    except Exception as e:
        log.warning(f"JWT verify failed: {e}")
        return None


# ============================
# OpenAI helpers (HTTP)
# ============================
def _openai_headers() -> Dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }


def openai_transcribe(ogg_bytes: bytes) -> str:
    """
    Uses OpenAI Audio Transcriptions API (multipart).
    """
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = _openai_headers()
    files = {
        "file": ("audio.ogg", ogg_bytes, "audio/ogg"),
        "model": (None, "gpt-4o-mini-transcribe"),
    }
    r = requests.post(url, headers=headers, files=files, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI transcribe failed: HTTP {r.status_code}: {r.text}")
    data = r.json()
    text = data.get("text") or ""
    return text.strip()


def openai_translate_text(text: str, target_lang: str) -> str:
    """
    Simple translation via Responses API.
    """
    url = "https://api.openai.com/v1/responses"
    headers = _openai_headers()
    headers["Content-Type"] = "application/json"

    prompt = (
        f"Translate the following text to {target_lang}.\n"
        f"Return only the translation, no quotes, no extra commentary.\n\n"
        f"Text:\n{text}"
    )
    payload = {
        "model": "gpt-4.1-mini",
        "input": prompt,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI translate failed: HTTP {r.status_code}: {r.text}")
    data = r.json()
    out = ""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    out += c.get("text", "")
    out = out.strip()
    return out or "(empty translation)"


def openai_tts(text: str) -> bytes:
    """
    TTS: return audio bytes (ogg/opus).
    """
    url = "https://api.openai.com/v1/audio/speech"
    headers = _openai_headers()
    headers["Content-Type"] = "application/json"

    payload = {
        "model": "gpt-4o-mini-tts",
        "voice": "alloy",
        "format": "ogg",
        "input": text,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI TTS failed: HTTP {r.status_code}: {r.text}")
    return r.content


# ============================
# Billing logic
# ============================
def decide_billing(user: User, voice_seconds: int) -> Tuple[str, int]:
    """
    Returns (mode, seconds_to_charge):
      mode in {"trial", "paid", "deny"}
    Trial: only if user.trial_left > 0 AND voice_seconds <= TRIAL_MAX_SECONDS
    Paid: if user.balance_seconds >= voice_seconds (or >= MIN_BILLABLE_SECONDS) -> charge full voice_seconds
    Deny: otherwise
    """
    voice_seconds = int(max(0, voice_seconds))
    if voice_seconds < MIN_BILLABLE_SECONDS:
        voice_seconds = MIN_BILLABLE_SECONDS

    if (user.trial_left or 0) > 0 and voice_seconds <= TRIAL_MAX_SECONDS:
        return "trial", 0

    bal = int(user.balance_seconds or 0)
    if bal >= voice_seconds:
        return "paid", voice_seconds

    return "deny", 0


def apply_billing(db, user: User, mode: str, seconds_to_charge: int):
    if mode == "trial":
        user.trial_left = max(0, int(user.trial_left or 0) - 1)
        user.trial_messages = int(user.trial_messages or 0) + 1
    elif mode == "paid":
        seconds_to_charge = max(0, int(seconds_to_charge))
        user.balance_seconds = max(0, int(user.balance_seconds or 0) - seconds_to_charge)

    user.updated_at = datetime.utcnow()
    db.add(user)


# ============================
# Support helpers
# ============================
def is_admin(chat_id: int) -> bool:
    try:
        return str(chat_id) == str(ADMIN_ID) and str(ADMIN_ID).strip() != ""
    except Exception:
        return False


def admin_notify(text: str):
    if not ADMIN_ID:
        return
    try:
        tg_send_message(int(ADMIN_ID), text)
    except Exception:
        pass


# ============================
# FastAPI
# ============================
app = FastAPI()


@app.on_event("startup")
def startup():
    init_db()
    log.info("Startup complete")


@app.get("/", response_class=HTMLResponse)
def landing():
    """Simple one-page landing for compliance/review."""
    bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else ""
    postback_url = f"{BASE_URL}{POSTBACK_PATH}" if BASE_URL else POSTBACK_PATH

    langs = ", ".join([name for name, _ in LANGS])
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lingovox — AI Voice Translator</title>
  <style>
    :root {{
      --bg:#0b1020; --card:#111a33; --text:#e9eefc; --muted:#a9b4d6; --accent:#7c5cff;
      --ok:#38d39f; --warn:#ffcc66; --border:rgba(255,255,255,.10);
    }}
    *{{box-sizing:border-box}}
    body{{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial; background:linear-gradient(180deg,#0b1020,#070a14); color:var(--text)}}
    a{{color:var(--text)}}
    .wrap{{max-width:980px;margin:0 auto;padding:28px 16px 60px}}
    .hero{{display:grid;gap:16px;grid-template-columns:1.4fr 1fr;align-items:stretch}}
    @media (max-width:860px){{.hero{{grid-template-columns:1fr}}}}
    .card{{background:rgba(17,26,51,.85); border:1px solid var(--border); border-radius:18px; padding:18px; box-shadow:0 10px 40px rgba(0,0,0,.25)}}
    h1{{margin:0 0 6px;font-size:34px;letter-spacing:-.02em}}
    h2{{margin:22px 0 10px;font-size:18px}}
    p{{margin:8px 0;color:var(--muted);line-height:1.55}}
    .badge{{display:inline-flex;gap:8px;align-items:center;border:1px solid var(--border);border-radius:999px;padding:6px 10px;color:var(--muted);font-size:13px}}
    .btn{{display:inline-flex;gap:10px;align-items:center;justify-content:center;
          padding:12px 14px;border-radius:12px;border:1px solid var(--border);
          background:rgba(124,92,255,.16); text-decoration:none}}
    .btn:hover{{background:rgba(124,92,255,.22)}}
    .grid{{display:grid;gap:12px;grid-template-columns:repeat(3,1fr)}}
    @media (max-width:860px){{.grid{{grid-template-columns:1fr}}}}
    .kpi{{padding:14px;border-radius:14px;border:1px solid var(--border);background:rgba(0,0,0,.15)}}
    .kpi b{{display:block;font-size:15px}}
    .kpi span{{color:var(--muted);font-size:13px}}
    .price{{display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border:1px solid var(--border);border-radius:14px;background:rgba(0,0,0,.15)}}
    .price b{{font-size:14px}}
    .footer{{margin-top:18px;color:var(--muted);font-size:12px;line-height:1.55}}
    code{{background:rgba(0,0,0,.25); padding:2px 6px; border-radius:8px; border:1px solid var(--border)}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="badge">🎙️ <span>Lingovox</span> <span>— AI Voice Translator for Telegram</span></div>

    <div class="hero" style="margin-top:14px;">
      <div class="card">
        <h1>Translate voice messages — instantly.</h1>
        <p>
          Lingovox is a Telegram bot that converts your voice message to text, translates it to your selected language,
          and replies with a natural-sounding voice message.
        </p>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:12px;">
          {f'<a class="btn" href="{bot_link}" target="_blank" rel="noreferrer">Open Telegram bot ↗</a>' if bot_link else '<span class="badge">Set <code>BOT_USERNAME</code> to show bot link</span>'}
          
        </div>

        <h2>Supported languages</h2>
        <p>{langs}</p>

        <h2>How it works</h2>
        <p>1) Choose target language → 2) Send voice → 3) Receive translated voice back.</p>
      </div>

      <div class="card">
        <h2>Pricing (minutes)</h2>
        <div class="price"><b>30 minutes</b><span>$3</span></div>
        <div style="height:10px"></div>
        <div class="price"><b>60 minutes</b><span>$8</span></div>
        <div style="height:10px"></div>
        <div class="price"><b>180 minutes</b><span>$20</span></div>
        <div style="height:10px"></div>
        <div class="price"><b>600 minutes</b><span>$50</span></div>

        <h2 style="margin-top:18px;">Free trial</h2>
        <p>New users get <b>{TRIAL_LIMIT}</b> free messages (each up to <b>{TRIAL_MAX_SECONDS} seconds</b>).</p>

        <h2 style="margin-top:18px;">Support</h2>
        <p>
          In the bot, use <code>/support</code> to create a support ticket.<br/>
          Admins can use <code>/stat</code> for statistics.
        </p>
      </div>
    </div>

    <div class="card" style="margin-top:14px;">
      <div class="grid">
        <div class="kpi"><b>Data & privacy</b><span>We process voice to generate translation and TTS. We do not sell personal data.</span></div>
        <div class="kpi"><b>Payments</b><span>Payments are processed by CryptoCloud. Minutes are credited after confirmation (postback).</span></div>
        <div class="kpi"><b>Reliability</b><span>Duplicate postbacks are ignored. Balance never goes negative.</span></div>
      </div>

      <div class="footer">
        <p><b>Terms.</b> Translations may contain errors. Service is provided “as is”.
        Refunds are handled case-by-case for duplicated charges or technical issues.</p>
      </div>
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=200)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()

    try:
        # ----------------------------
        # MESSAGE
        # ----------------------------
        if "message" in update:
            msg = update["message"]
            chat_id = msg.get("chat", {}).get("id")
            text = (msg.get("text") or "").strip()

            if not chat_id:
                return JSONResponse({"ok": True})

            # /start
            if text == "/start":
                with SessionLocal() as db:
                    user = db.get(User, int(chat_id))
                    if not user:
                        user = User(
                            telegram_id=int(chat_id),
                            target_lang="en",
                            trial_left=TRIAL_LIMIT,
                            trial_messages=0,
                            balance_seconds=0,
                        )
                        db.add(user)
                        db.commit()
                        db.refresh(user)

                    kb = build_main_keyboard(user.target_lang)
                    tg_send_message(chat_id, format_status_text(user), reply_markup=kb)
                return JSONResponse({"ok": True})

            # /buy
            if text == "/buy":
                tg_send_message(chat_id, "💳 Choose a minutes package:", reply_markup=build_packages_keyboard())
                return JSONResponse({"ok": True})

            # /support
            if text.startswith("/support"):
                tg_send_message(
                    chat_id,
                    "🆘 Support\n\nSend your message in this format:\n/support your text here\n\nExample:\n/support Payment credited, but minutes did not appear."
                )
                return JSONResponse({"ok": True})

            # /stat (admin only)
            if text == "/stat":
                if not is_admin(int(chat_id)):
                    tg_send_message(chat_id, "⛔ This command is for admin only.")
                    return JSONResponse({"ok": True})

                with SessionLocal() as db:
                    users_count = db.query(User).count()
                    payments_count = db.query(Payment).count()
                    paid_count = db.query(Payment).filter(Payment.status.in_(["paid", "success"])).count()
                    open_tickets = db.query(SupportTicket).filter(SupportTicket.status == "open").count()

                tg_send_message(
                    chat_id,
                    "📊 Stats\n\n"
                    f"Users: {users_count}\n"
                    f"Payments: {payments_count}\n"
                    f"Paid: {paid_count}\n"
                    f"Open tickets: {open_tickets}\n"
                )
                return JSONResponse({"ok": True})

            # admin reply to ticket: /reply <ticket_id> <text>
            if text.startswith("/reply"):
                if not is_admin(int(chat_id)):
                    tg_send_message(chat_id, "⛔ This command is for admin only.")
                    return JSONResponse({"ok": True})

                parts = text.split(maxsplit=2)
                if len(parts) < 3:
                    tg_send_message(chat_id, "Usage: /reply <ticket_id> <message>")
                    return JSONResponse({"ok": True})

                ticket_id = int(parts[1])
                reply_text = parts[2].strip()

                with SessionLocal() as db:
                    ticket = db.get(SupportTicket, ticket_id)
                    if not ticket:
                        tg_send_message(chat_id, "Ticket not found.")
                        return JSONResponse({"ok": True})

                    # send message to user
                    tg_send_message(
                        int(ticket.telegram_id),
                        f"✅ Support reply (ticket #{ticket.id}):\n{reply_text}"
                    )

                    ticket.status = "closed"
                    ticket.updated_at = datetime.utcnow()
                    db.add(ticket)
                    db.commit()

                tg_send_message(chat_id, f"✅ Replied and closed ticket #{ticket_id}.")
                return JSONResponse({"ok": True})

            # Create ticket: "/support ..."
            if text.lower().startswith("/support "):
                ticket_text = text.split(" ", 1)[1].strip()
                if not ticket_text:
                    tg_send_message(chat_id, "Please add a message. Example: /support I need help")
                    return JSONResponse({"ok": True})

                with SessionLocal() as db:
                    ticket = SupportTicket(
                        telegram_id=int(chat_id),
                        message=ticket_text,
                        status="open",
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                    db.add(ticket)
                    db.commit()
                    db.refresh(ticket)

                tg_send_message(chat_id, f"✅ Ticket created: #{ticket.id}\nWe will reply soon.")

                admin_notify(
                    "🆘 New support ticket\n\n"
                    f"Ticket: #{ticket.id}\n"
                    f"User: {chat_id}\n"
                    f"Message: {ticket_text}"
                )
                return JSONResponse({"ok": True})

            # Handle VOICE
            if "voice" in msg:
                voice = msg["voice"]
                file_id = voice.get("file_id")
                duration = int(voice.get("duration") or 0)

                if not file_id:
                    return JSONResponse({"ok": True})

                # Ensure user exists
                with SessionLocal() as db:
                    user = db.get(User, int(chat_id))
                    if not user:
                        user = User(
                            telegram_id=int(chat_id),
                            target_lang="en",
                            trial_left=TRIAL_LIMIT,
                            trial_messages=0,
                            balance_seconds=0,
                        )
                        db.add(user)
                        db.commit()
                        db.refresh(user)

                    # billing decision
                    mode, seconds_to_charge = decide_billing(user, duration)
                    if mode == "deny":
                        kb = build_main_keyboard(user.target_lang)
                        bal_min = max(0, int(user.balance_seconds or 0)) // 60
                        tg_send_message(
                            chat_id,
                            "⛔ Not enough balance.\n\n"
                            f"Your balance: {bal_min} min\n"
                            f"Free messages left: {user.trial_left}\n\n"
                            "Tap “Buy minutes” to top up.",
                            reply_markup=kb
                        )
                        return JSONResponse({"ok": True})

                # Download voice from Telegram
                # 1) getFile
                gf = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30).json()
                if not gf.get("ok"):
                    tg_send_message(chat_id, f"Failed to get file: {gf}")
                    return JSONResponse({"ok": True})

                file_path = gf["result"]["file_path"]
                file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
                audio = requests.get(file_url, timeout=60).content

                # OpenAI pipeline
                try:
                    original_text = openai_transcribe(audio)
                    translated_text = openai_translate_text(original_text, user.target_lang)
                    tts_audio = openai_tts(translated_text)
                except Exception as e:
                    log.exception("Voice pipeline error")
                    tg_send_message(chat_id, f"⚠️ Error while processing voice: {e}")
                    return JSONResponse({"ok": True})

                # Apply billing after successful processing
                with SessionLocal() as db:
                    user = db.get(User, int(chat_id))
                    if not user:
                        user = User(
                            telegram_id=int(chat_id),
                            target_lang="en",
                            trial_left=TRIAL_LIMIT,
                            trial_messages=0,
                            balance_seconds=0,
                        )
                        db.add(user)
                        db.commit()
                        db.refresh(user)

                    mode, seconds_to_charge = decide_billing(user, duration)
                    if mode == "deny":
                        # race condition safety
                        kb = build_main_keyboard(user.target_lang)
                        tg_send_message(chat_id, "⛔ Not enough balance (re-check).", reply_markup=kb)
                        return JSONResponse({"ok": True})

                    apply_billing(db, user, mode, seconds_to_charge)
                    db.commit()
                    db.refresh(user)

                    kb = build_main_keyboard(user.target_lang)
                    caption = None
                    if mode == "trial":
                        caption = f"🎁 Trial message used. Free left: {user.trial_left}"
                    elif mode == "paid":
                        caption = f"💳 Charged: {seconds_to_charge}s. Balance: {max(0, int(user.balance_seconds))//60} min"

                # Send voice response
                tg_send_voice(chat_id, tts_audio, caption=caption)
                # Also update status menu (optional)
                tg_send_message(chat_id, format_status_text(user), reply_markup=kb)
                return JSONResponse({"ok": True})

            return JSONResponse({"ok": True})

        # ----------------------------
        # CALLBACK
        # ----------------------------
        if "callback_query" in update:
            cq = update["callback_query"]
            data = cq.get("data", "")
            chat_id = cq.get("message", {}).get("chat", {}).get("id")
            cq_id = cq.get("id")

            if not chat_id:
                return JSONResponse({"ok": True})

            # language switch
            if data.startswith("lang:"):
                lang = data.split(":", 1)[1].strip()
                with SessionLocal() as db:
                    user = db.get(User, int(chat_id))
                    if not user:
                        user = User(telegram_id=int(chat_id), target_lang="en", trial_left=TRIAL_LIMIT)
                        db.add(user)
                        db.commit()
                        db.refresh(user)

                    user.target_lang = lang
                    user.updated_at = datetime.utcnow()
                    db.add(user)
                    db.commit()
                    db.refresh(user)

                    kb = build_main_keyboard(user.target_lang)
                    tg_send_message(chat_id, format_status_text(user), reply_markup=kb)

                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

            # support menu
            if data == "support:menu":
                tg_send_message(
                    chat_id,
                    "🆘 Support\n\nTo create a ticket, send:\n/support your message\n\nExample:\n/support I can’t pay / minutes not credited."
                )
                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

            # buy menu
            if data == "buy:menu":
                tg_send_message(chat_id, "💳 Choose a minutes package:", reply_markup=build_packages_keyboard())
                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

            if data == "buy:back":
                with SessionLocal() as db:
                    user = db.get(User, int(chat_id))
                    if not user:
                        user = User(telegram_id=int(chat_id), target_lang="en", trial_left=TRIAL_LIMIT)
                        db.add(user)
                        db.commit()
                        db.refresh(user)
                kb = build_main_keyboard(user.target_lang)
                tg_send_message(chat_id, format_status_text(user), reply_markup=kb)
                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

            # buy package
            if data.startswith("buy:"):
                package_code = data.split(":", 1)[1].strip()
                if package_code not in PACKAGES:
                    tg_send_message(chat_id, "Unknown package.")
                    tg_answer_callback(cq_id)
                    return JSONResponse({"ok": True})

                missing = env_missing()
                if missing:
                    tg_send_message(chat_id, f"Missing env vars: {', '.join(missing)}")
                    tg_answer_callback(cq_id)
                    return JSONResponse({"ok": True})

                amount_usd = PACKAGES[package_code]["usd"]
                order_id = f"{chat_id}_{package_code}_{int(time.time())}"
                description = f"Minutes package {package_code} for user {chat_id}"

                cc = cryptocloud_create_invoice(order_id=order_id, amount_usd=amount_usd, description=description)
                if not cc["ok"]:
                    tg_send_message(
                        chat_id,
                        f"Invoice create failed: {CC_CREATE_INVOICE_URL}\nHTTP {cc.get('status')}\n{cc.get('raw') or cc.get('data')}"
                    )
                    tg_answer_callback(cq_id)
                    return JSONResponse({"ok": True})

                data_json = cc["data"]
                result = data_json.get("result") or data_json.get("data") or data_json
                invoice_uuid = result.get("uuid") or result.get("invoice_id") or result.get("id")
                pay_url = result.get("link") or result.get("pay_url") or result.get("url")

                if not invoice_uuid:
                    tg_send_message(chat_id, f"CryptoCloud response without invoice id:\n{data_json}")
                    tg_answer_callback(cq_id)
                    return JSONResponse({"ok": True})

                # store payment (invoice_id is NOT NULL!)
                with SessionLocal() as db:
                    try:
                        p = Payment(
                            telegram_id=int(chat_id),
                            order_id=order_id,
                            invoice_id=str(invoice_uuid),
                            package_code=package_code,
                            amount_usd=int(amount_usd),
                            status="created",
                            created_at=datetime.utcnow(),
                            updated_at=datetime.utcnow(),
                        )
                        db.add(p)
                        db.commit()
                    except IntegrityError as e:
                        db.rollback()
                        log.warning(f"Payment insert IntegrityError: {e}")
                    except Exception as e:
                        db.rollback()
                        tg_send_message(chat_id, f"DB error: {e}")
                        tg_answer_callback(cq_id)
                        return JSONResponse({"ok": True})

                if pay_url:
                    kb = {
                        "inline_keyboard": [
                            [{"text": "Go to payment ✅", "url": pay_url}],
                        ]
                    }
                    tg_send_message(chat_id, f"✅ Invoice created.\nAmount: ${amount_usd}\nPackage: {package_code}", reply_markup=kb)
                else:
                    tg_send_message(chat_id, f"✅ Invoice created: {invoice_uuid}\n(No payment link in response)")

                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

            tg_answer_callback(cq_id)
            return JSONResponse({"ok": True})

        return JSONResponse({"ok": True})

    except Exception as e:
        log.exception("telegram_webhook error")
        return JSONResponse({"ok": True, "error": str(e)})


@app.post(POSTBACK_PATH)
async def cryptocloud_postback(req: Request):
    raw = await req.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        try:
            payload = json.loads(raw)
        except Exception:
            return PlainTextResponse("bad json", status_code=200)

    try:
        status = (payload.get("status") or "").lower()
        order_id = payload.get("order_id")
        token = payload.get("token")

        if not token:
            return PlainTextResponse("no token", status_code=200)

        decoded = verify_postback_token(token)
        if not decoded:
            return PlainTextResponse("bad token", status_code=200)

        token_invoice_id = decoded.get("id")

        postback_invoice_id = payload.get("invoice_id")
        invoice_info = payload.get("invoice_info") or {}
        invoice_uuid = invoice_info.get("uuid")

        effective_invoice_id = invoice_uuid or postback_invoice_id or token_invoice_id

        is_paid = status in ("success", "paid")
        invoice_status = (invoice_info.get("invoice_status") or "").lower()
        if invoice_status in ("success", "paid"):
            is_paid = True

        with SessionLocal() as db:
            p = None
            if order_id:
                p = db.query(Payment).filter(Payment.order_id == order_id).first()
            if not p and effective_invoice_id:
                p = db.query(Payment).filter(Payment.invoice_id == str(effective_invoice_id)).first()

            if not p:
                log.warning(f"Payment not found for order_id={order_id} invoice_id={effective_invoice_id}")
                return PlainTextResponse("payment not found", status_code=200)

            if (p.status or "").lower() in ("paid", "success"):
                log.info("Already paid, skip")
                return PlainTextResponse("ok", status_code=200)

            if not is_paid:
                p.status = status or "unknown"
                p.updated_at = datetime.utcnow()
                db.add(p)
                db.commit()
                return PlainTextResponse("ok", status_code=200)

            pkg = PACKAGES.get(p.package_code)
            if not pkg:
                p.status = "paid"
                p.updated_at = datetime.utcnow()
                db.add(p)
                db.commit()
                return PlainTextResponse("ok", status_code=200)

            add_seconds = int(pkg["minutes"] * 60)

            user = db.get(User, int(p.telegram_id))
            if not user:
                user = User(
                    telegram_id=int(p.telegram_id),
                    target_lang="en",
                    trial_left=TRIAL_LIMIT,
                    trial_messages=0,
                    balance_seconds=0,
                )
                db.add(user)
                db.commit()
                db.refresh(user)

            user.balance_seconds = max(0, int(user.balance_seconds or 0) + add_seconds)
            user.updated_at = datetime.utcnow()

            p.status = "paid"
            p.updated_at = datetime.utcnow()

            db.add(user)
            db.add(p)
            db.commit()
            db.refresh(user)

            bal_min = max(0, int(user.balance_seconds or 0)) // 60
            tg_send_message(
                int(user.telegram_id),
                f"✅ Payment received!\nPackage: {p.package_code}\nCredited: {pkg['minutes']} min\nBalance: {bal_min} min",
                reply_markup=build_main_keyboard(user.target_lang),
            )

        return PlainTextResponse("ok", status_code=200)

    except Exception as e:
        log.exception("postback error")
        return PlainTextResponse(f"error: {e}", status_code=200)
