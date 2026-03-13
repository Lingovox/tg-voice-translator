import os
import time
import json
import logging
import hashlib
import hmac
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

import requests

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean, DateTime, text as sql_text
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

NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY", "").strip()
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "").strip()

TRIAL_LIMIT = int(os.getenv("TRIAL_LIMIT", "5"))
TRIAL_MAX_SECONDS = int(os.getenv("TRIAL_MAX_SECONDS", "60"))
MIN_BILLABLE_SECONDS = int(os.getenv("MIN_BILLABLE_SECONDS", "1"))


# ============================
# Constants
# ============================
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

NP_CREATE_INVOICE_URL = "https://api.nowpayments.io/v1/invoice"
POSTBACK_PATH = "/payments/nowpayments"

PACKAGES = {
"P30": {"usd": 10, "minutes": 30},
"P60": {"usd": 15, "minutes": 60},
"P180": {"usd": 30, "minutes": 180},
"P600": {"usd": 70, "minutes": 600},
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


CONVERSATION_PAIRS = {
    "ru_es": {"a": "ru", "b": "es", "label": "Русский ↔ Español"},
    "ru_en": {"a": "ru", "b": "en", "label": "Русский ↔ English"},
    "ru_de": {"a": "ru", "b": "de", "label": "Русский ↔ Deutsch"},
}

LANGUAGE_NAMES = {code: title for title, code in LANGS}


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
    trial_left = Column(Integer, nullable=False, default=TRIAL_LIMIT)
    trial_messages = Column(Integer, nullable=False, default=0)
    balance_seconds = Column(Integer, nullable=False, default=0)
    is_subscribed = Column(Boolean, nullable=False, default=False)
    mode = Column(String, nullable=False, default="translate")
    conversation_pair = Column(String, nullable=False, default="ru_es")

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
    status = Column(String, nullable=False, default="created")

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    message = Column(String, nullable=False)
    status = Column(String, nullable=False, default="open")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)
    ensure_user_columns()


def ensure_user_columns():
    """Add new columns for existing deployments without migrations."""
    statements = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS mode VARCHAR DEFAULT 'translate' NOT NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS conversation_pair VARCHAR DEFAULT 'ru_es' NOT NULL",
    ]
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(sql_text(stmt))


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


def build_main_keyboard(selected_lang: str, mode: str = "translate", pair_code: str = "ru_es") -> Dict[str, Any]:
    mode_prefix = "✅ " if mode == "conversation" else ""
    pair_label = CONVERSATION_PAIRS.get(pair_code, CONVERSATION_PAIRS["ru_es"])["label"]

    rows = [
        [{"text": f"{mode_prefix}🗣 Conversation mode", "callback_data": "mode:conversation"}],
        [{"text": f"🌐 Pair: {pair_label}", "callback_data": "pair:menu"}],
    ]

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
            [{"text": "30 min — $10", "callback_data": "buy:P30"}],
            [{"text": "60 min — $15", "callback_data": "buy:P60"}],
            [{"text": "180 min — $30", "callback_data": "buy:P180"}],
            [{"text": "600 min — $70", "callback_data": "buy:P600"}],
            [{"text": "⬅️ Back", "callback_data": "buy:back"}],
        ]
    }
        


def build_pair_keyboard(selected_pair: str) -> Dict[str, Any]:
    rows = []
    for pair_code, pair in CONVERSATION_PAIRS.items():
        prefix = "✅ " if pair_code == selected_pair else ""
        rows.append([{"text": f"{prefix}{pair['label']}", "callback_data": f"pair:{pair_code}"}])
    rows.append([{"text": "⬅️ Back", "callback_data": "pair:back"}])
    return {"inline_keyboard": rows}


def get_or_create_user(db, telegram_id: int) -> User:
    user = db.get(User, int(telegram_id))
    if not user:
        user = User(
            telegram_id=int(telegram_id),
            target_lang="en",
            trial_left=TRIAL_LIMIT,
            trial_messages=0,
            balance_seconds=0,
            mode="translate",
            conversation_pair="ru_es",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def pair_label(pair_code: str) -> str:
    return CONVERSATION_PAIRS.get(pair_code, CONVERSATION_PAIRS["ru_es"])["label"]


def format_mode_label(mode: str) -> str:
    return "conversation" if mode == "conversation" else "translate"


def resolve_conversation_target(detected_lang: str, pair_code: str) -> Tuple[Optional[str], Optional[str]]:
    pair = CONVERSATION_PAIRS.get(pair_code, CONVERSATION_PAIRS["ru_es"])
    lang_a = pair["a"]
    lang_b = pair["b"]
    if detected_lang == lang_a:
        return lang_a, lang_b
    if detected_lang == lang_b:
        return lang_b, lang_a
    return None, None


def parse_transcription_result(payload: Dict[str, Any]) -> Tuple[str, str]:
    text_value = (payload.get("text") or "").strip()
    language = str(payload.get("language") or "").strip().lower()
    return text_value, language


def format_status_text(user: User) -> str:
    bal_min = max(0, int(user.balance_seconds or 0)) // 60
    mode_line = "🗣 Mode: Conversation" if user.mode == "conversation" else "🎤 Mode: Voice translate"
    pair_line = f"🔀 Conversation pair: {pair_label(user.conversation_pair)}"
    return (
        "🎙 Lingovox — AI voice translator\n\n"
        f"{mode_line}\n"
        f"{pair_line}\n"
        f"🌍 Target language: {user.target_lang}\n"
        f"🎁 Free messages left: {user.trial_left} (≤ {TRIAL_MAX_SECONDS}s)\n"
        f"💳 Balance: {bal_min} min\n\n"
        "Send a voice message — I'll translate and reply with voice."
    )


# ============================
# NOWPayments helpers
# ============================
def env_missing() -> list:
    missing = []
    if not NOWPAYMENTS_API_KEY:
        missing.append("NOWPAYMENTS_API_KEY")
    if not NOWPAYMENTS_IPN_SECRET:
        missing.append("NOWPAYMENTS_IPN_SECRET")
    if not BASE_URL:
        missing.append("BASE_URL")
    return missing


def nowpayments_create_invoice(order_id: str, amount_usd: int, description: str) -> Dict[str, Any]:
    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json",
    }

    payload = {
        "price_amount": amount_usd,
        "price_currency": "usd",
        "pay_currency": "usdttrc20",
        "order_id": order_id,
        "order_description": description,
        "ipn_callback_url": f"{BASE_URL}{POSTBACK_PATH}",
        "success_url": f"{BASE_URL}/",
        "cancel_url": f"{BASE_URL}/",
        "is_fixed_rate": False,
        "is_fee_paid_by_user": False,
    }

    r = requests.post(NP_CREATE_INVOICE_URL, headers=headers, json=payload, timeout=30)

    ct = (r.headers.get("content-type") or "").lower()
    if "application/json" not in ct:
        return {"ok": False, "status": r.status_code, "raw": r.text}

    data = r.json()
    return {"ok": r.status_code in (200, 201), "status": r.status_code, "data": data}


def verify_nowpayments_signature(raw_body: bytes, signature: str) -> bool:
    if not NOWPAYMENTS_IPN_SECRET or not signature:
        return False

    calculated = hmac.new(
        NOWPAYMENTS_IPN_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha512,
    ).hexdigest()

    return hmac.compare_digest(calculated, signature)


# ============================
# OpenAI helpers
# ============================
def _openai_headers() -> Dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }


def openai_transcribe(ogg_bytes: bytes) -> Dict[str, Any]:
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
    return data


def openai_translate_text(text: str, target_lang: str, source_lang: Optional[str] = None) -> str:
    url = "https://api.openai.com/v1/responses"
    headers = _openai_headers()
    headers["Content-Type"] = "application/json"

    prompt = (
        f"Translate the following text"
        f" from {source_lang if source_lang else 'auto-detected source language'}"
        f" to {target_lang}.\n"
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
    bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else ""
    langs = ", ".join([name for name, _ in LANGS])

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lingovox — AI Voice Translator</title>
  <style>
    :root {{
      --bg:#0b1020; --card:#111a33; --text:#e9eefc; --muted:#a9b4d6; --border:rgba(255,255,255,.10);
    }}
    *{{box-sizing:border-box}}
    body{{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial;background:linear-gradient(180deg,#0b1020,#070a14);color:var(--text)}}
    a{{color:var(--text)}}
    .wrap{{max-width:980px;margin:0 auto;padding:28px 16px 60px}}
    .hero{{display:grid;gap:16px;grid-template-columns:1.4fr 1fr;align-items:stretch}}
    @media (max-width:860px){{.hero{{grid-template-columns:1fr}}}}
    .card{{background:rgba(17,26,51,.85);border:1px solid var(--border);border-radius:18px;padding:18px;box-shadow:0 10px 40px rgba(0,0,0,.25)}}
    h1{{margin:0 0 6px;font-size:34px;letter-spacing:-.02em}}
    h2{{margin:22px 0 10px;font-size:18px}}
    p{{margin:8px 0;color:var(--muted);line-height:1.55}}
    .badge{{display:inline-flex;gap:8px;align-items:center;border:1px solid var(--border);border-radius:999px;padding:6px 10px;color:var(--muted);font-size:13px}}
    .btn{{display:inline-flex;gap:10px;align-items:center;justify-content:center;padding:12px 14px;border-radius:12px;border:1px solid var(--border);background:rgba(124,92,255,.16);text-decoration:none}}
    .btn:hover{{background:rgba(124,92,255,.22)}}
    .grid{{display:grid;gap:12px;grid-template-columns:repeat(3,1fr)}}
    @media (max-width:860px){{.grid{{grid-template-columns:1fr}}}}
    .kpi{{padding:14px;border-radius:14px;border:1px solid var(--border);background:rgba(0,0,0,.15)}}
    .kpi b{{display:block;font-size:15px}}
    .kpi span{{color:var(--muted);font-size:13px}}
    .price{{display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border:1px solid var(--border);border-radius:14px;background:rgba(0,0,0,.15)}}
    .footer{{margin-top:18px;color:var(--muted);font-size:12px;line-height:1.55}}
    code{{background:rgba(0,0,0,.25);padding:2px 6px;border-radius:8px;border:1px solid var(--border)}}
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
        <p>In the bot, use <code>/support</code> to create a support ticket.<br/>Admins can use <code>/stat</code> for statistics.</p>
      </div>
    </div>

    <div class="card" style="margin-top:14px;">
      <div class="grid">
        <div class="kpi"><b>Data & privacy</b><span>We process voice to generate translation and TTS. We do not sell personal data.</span></div>
        <div class="kpi"><b>Payments</b><span>Payments are processed by NOWPayments. Minutes are credited after payment confirmation.</span></div>
        <div class="kpi"><b>Reliability</b><span>Duplicate payment notifications are ignored. Balance never goes negative.</span></div>
      </div>

      <div class="footer">
        <p><b>Terms.</b> Translations may contain errors. Service is provided “as is”. Refunds are handled case-by-case for duplicated charges or technical issues.</p>
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
        if "message" in update:
            msg = update["message"]
            chat_id = msg.get("chat", {}).get("id")
            text = (msg.get("text") or "").strip()

            if not chat_id:
                return JSONResponse({"ok": True})

            if text == "/start":
                with SessionLocal() as db:
                    user = get_or_create_user(db, int(chat_id))
                    kb = build_main_keyboard(user.target_lang, user.mode, user.conversation_pair)
                    tg_send_message(chat_id, format_status_text(user), reply_markup=kb)
                return JSONResponse({"ok": True})

            if text == "/buy":
                tg_send_message(chat_id, "💳 Choose a minutes package:", reply_markup=build_packages_keyboard())
                return JSONResponse({"ok": True})

            if text.startswith("/support"):
                tg_send_message(
                    chat_id,
                    "🆘 Support\n\nSend your message in this format:\n/support your text here\n\nExample:\n/support Payment credited, but minutes did not appear."
                )
                return JSONResponse({"ok": True})

            if text == "/stat":
                if not is_admin(int(chat_id)):
                    tg_send_message(chat_id, "⛔ This command is for admin only.")
                    return JSONResponse({"ok": True})

                with SessionLocal() as db:
                    users_count = db.query(User).count()
                    payments_count = db.query(Payment).count()
                    paid_count = db.query(Payment).filter(Payment.status.in_(["paid", "finished", "success"])).count()
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

            if "voice" in msg:
                voice = msg["voice"]
                file_id = voice.get("file_id")
                duration = int(voice.get("duration") or 0)

                if not file_id:
                    return JSONResponse({"ok": True})

                with SessionLocal() as db:
                    user = get_or_create_user(db, int(chat_id))

                    mode, seconds_to_charge = decide_billing(user, duration)
                    if mode == "deny":
                        kb = build_main_keyboard(user.target_lang, user.mode, user.conversation_pair)
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

                gf = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30).json()
                if not gf.get("ok"):
                    tg_send_message(chat_id, f"Failed to get file: {gf}")
                    return JSONResponse({"ok": True})

                file_path = gf["result"]["file_path"]
                file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
                audio = requests.get(file_url, timeout=60).content

                try:
                    with SessionLocal() as db:
                        user = get_or_create_user(db, int(chat_id))
                        target_lang = user.target_lang
                        pair_code = user.conversation_pair
                        user_mode = user.mode

                    transcription = openai_transcribe(audio)
                    original_text, detected_lang = parse_transcription_result(transcription)
                    if not original_text:
                        raise RuntimeError("Speech was not recognized")

                    if user_mode == "conversation":
                        source_lang, conv_target_lang = resolve_conversation_target(detected_lang, pair_code)
                        if not source_lang or not conv_target_lang:
                            raise RuntimeError(
                                f"Detected language '{detected_lang or 'unknown'}' is outside selected pair {pair_label(pair_code)}"
                            )
                        translated_text = openai_translate_text(original_text, conv_target_lang, source_lang=source_lang)
                    else:
                        translated_text = openai_translate_text(original_text, target_lang, source_lang=detected_lang or None)
                    tts_audio = openai_tts(translated_text)
                except Exception as e:
                    log.exception("Voice pipeline error")
                    tg_send_message(chat_id, f"⚠️ Error while processing voice: {e}")
                    return JSONResponse({"ok": True})

                with SessionLocal() as db:
                    user = get_or_create_user(db, int(chat_id))

                    mode, seconds_to_charge = decide_billing(user, duration)
                    if mode == "deny":
                        kb = build_main_keyboard(user.target_lang, user.mode, user.conversation_pair)
                        tg_send_message(chat_id, "⛔ Not enough balance (re-check).", reply_markup=kb)
                        return JSONResponse({"ok": True})

                    apply_billing(db, user, mode, seconds_to_charge)
                    db.commit()
                    db.refresh(user)

                    kb = build_main_keyboard(user.target_lang, user.mode, user.conversation_pair)
                    caption = None
                    if mode == "trial":
                        caption = f"🎁 Trial message used. Free left: {user.trial_left}"
                    elif mode == "paid":
                        caption = f"💳 Charged: {seconds_to_charge}s. Balance: {max(0, int(user.balance_seconds)) // 60} min"

                    info_text = None
                    if user.mode == "conversation":
                        info_text = (
                            f"🗣 Conversation mode\n"
                            f"Pair: {pair_label(user.conversation_pair)}\n"
                            f"Detected language: {detected_lang or 'unknown'}\n\n"
                            f"Original:\n{original_text}\n\n"
                            f"Translation:\n{translated_text}"
                        )

                tg_send_voice(chat_id, tts_audio, caption=caption)
                if info_text:
                    tg_send_message(chat_id, info_text)
                tg_send_message(chat_id, format_status_text(user), reply_markup=kb)
                return JSONResponse({"ok": True})

            return JSONResponse({"ok": True})

        if "callback_query" in update:
            cq = update["callback_query"]
            data = cq.get("data", "")
            chat_id = cq.get("message", {}).get("chat", {}).get("id")
            cq_id = cq.get("id")

            if not chat_id:
                return JSONResponse({"ok": True})

            if data.startswith("lang:"):
                lang = data.split(":", 1)[1].strip()
                with SessionLocal() as db:
                    user = get_or_create_user(db, int(chat_id))

                    user.target_lang = lang
                    user.updated_at = datetime.utcnow()
                    db.add(user)
                    db.commit()
                    db.refresh(user)

                    kb = build_main_keyboard(user.target_lang, user.mode, user.conversation_pair)
                    tg_send_message(chat_id, format_status_text(user), reply_markup=kb)

                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

            if data == "mode:conversation":
                with SessionLocal() as db:
                    user = get_or_create_user(db, int(chat_id))
                    user.mode = "conversation"
                    user.updated_at = datetime.utcnow()
                    db.add(user)
                    db.commit()
                    db.refresh(user)

                    kb = build_main_keyboard(user.target_lang, user.mode, user.conversation_pair)
                    tg_send_message(
                        chat_id,
                        "🗣 Conversation mode enabled.\n\n"
                        f"Selected pair: {pair_label(user.conversation_pair)}\n"
                        "Now two people can speak one by one using one phone. "
                        "The bot will detect which language from the selected pair was spoken and reply in the opposite language.",
                        reply_markup=kb,
                    )
                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

            if data == "pair:menu":
                with SessionLocal() as db:
                    user = get_or_create_user(db, int(chat_id))
                tg_send_message(chat_id, "🌐 Choose a conversation pair:", reply_markup=build_pair_keyboard(user.conversation_pair))
                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

            if data == "pair:back":
                with SessionLocal() as db:
                    user = get_or_create_user(db, int(chat_id))
                tg_send_message(chat_id, format_status_text(user), reply_markup=build_main_keyboard(user.target_lang, user.mode, user.conversation_pair))
                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

            if data.startswith("pair:"):
                pair_code = data.split(":", 1)[1].strip()
                if pair_code not in CONVERSATION_PAIRS:
                    tg_answer_callback(cq_id, "Unknown pair")
                    return JSONResponse({"ok": True})

                with SessionLocal() as db:
                    user = get_or_create_user(db, int(chat_id))
                    user.conversation_pair = pair_code
                    user.updated_at = datetime.utcnow()
                    db.add(user)
                    db.commit()
                    db.refresh(user)

                tg_send_message(
                    chat_id,
                    f"✅ Conversation pair updated: {pair_label(pair_code)}",
                    reply_markup=build_main_keyboard(user.target_lang, user.mode, user.conversation_pair),
                )
                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

            if data == "support:menu":
                tg_send_message(
                    chat_id,
                    "🆘 Support\n\nTo create a ticket, send:\n/support your message\n\nExample:\n/support I can’t pay / minutes not credited."
                )
                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

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
                kb = build_main_keyboard(user.target_lang, user.mode, user.conversation_pair)
                tg_send_message(chat_id, format_status_text(user), reply_markup=kb)
                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

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

                np = nowpayments_create_invoice(
                    order_id=order_id,
                    amount_usd=amount_usd,
                    description=description,
                )

                if not np["ok"]:
                    tg_send_message(
                        chat_id,
                        f"Invoice create failed\nHTTP {np.get('status')}\n{np.get('raw') or np.get('data')}"
                    )
                    tg_answer_callback(cq_id)
                    return JSONResponse({"ok": True})

                data_json = np["data"]
                invoice_id = (
                    data_json.get("id")
                    or data_json.get("invoice_id")
                    or data_json.get("payment_id")
                    or data_json.get("token_id")
                )
                pay_url = (
                    data_json.get("invoice_url")
                    or data_json.get("pay_url")
                    or data_json.get("url")
                )

                if not invoice_id:
                    tg_send_message(chat_id, f"NOWPayments response without invoice id:\n{data_json}")
                    tg_answer_callback(cq_id)
                    return JSONResponse({"ok": True})

                with SessionLocal() as db:
                    try:
                        p = Payment(
                            telegram_id=int(chat_id),
                            order_id=order_id,
                            invoice_id=str(invoice_id),
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
                    tg_send_message(
                        chat_id,
                        f"✅ Invoice created.\nAmount: ${amount_usd}\nPackage: {package_code}",
                        reply_markup=kb
                    )
                else:
                    tg_send_message(chat_id, f"✅ Invoice created: {invoice_id}\n(No payment link in response)")

                tg_answer_callback(cq_id)
                return JSONResponse({"ok": True})

            tg_answer_callback(cq_id)
            return JSONResponse({"ok": True})

        return JSONResponse({"ok": True})

    except Exception as e:
        log.exception("telegram_webhook error")
        return JSONResponse({"ok": True, "error": str(e)})


@app.post(POSTBACK_PATH)
async def nowpayments_postback(req: Request):
    raw = await req.body()
    signature = req.headers.get("x-nowpayments-sig", "")

    if not verify_nowpayments_signature(raw, signature):
        log.warning("Invalid NOWPayments signature")
        return PlainTextResponse("invalid signature", status_code=200)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return PlainTextResponse("bad json", status_code=200)

    try:
        payment_status = str(payload.get("payment_status") or "").lower()
        order_id = payload.get("order_id")
        payment_id = payload.get("payment_id") or payload.get("id") or payload.get("invoice_id")

        paid_statuses = {"finished", "confirmed", "sending", "partially_paid", "paid"}

        with SessionLocal() as db:
            p = None
            if order_id:
                p = db.query(Payment).filter(Payment.order_id == str(order_id)).first()
            if not p and payment_id:
                p = db.query(Payment).filter(Payment.invoice_id == str(payment_id)).first()

            if not p:
                log.warning(f"Payment not found for order_id={order_id}, payment_id={payment_id}")
                return PlainTextResponse("payment not found", status_code=200)

            if (p.status or "").lower() in ("paid", "finished", "success", "confirmed"):
                log.info("Already credited, skip duplicate IPN")
                return PlainTextResponse("ok", status_code=200)

            p.status = payment_status or "unknown"
            p.updated_at = datetime.utcnow()
            db.add(p)
            db.commit()

            if payment_status not in paid_statuses:
                return PlainTextResponse("ok", status_code=200)

            pkg = PACKAGES.get(p.package_code)
            if not pkg:
                return PlainTextResponse("ok", status_code=200)

            add_seconds = int(pkg["minutes"] * 60)

            user = get_or_create_user(db, int(p.telegram_id))

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
                reply_markup=build_main_keyboard(user.target_lang, user.mode, user.conversation_pair),
            )

        return PlainTextResponse("ok", status_code=200)

    except Exception as e:
        log.exception("NOWPayments postback error")
        return PlainTextResponse(f"error: {e}", status_code=200)


@app.get("/terms", response_class=HTMLResponse)
def terms():
    return """
    <!doctype html>
    <html>
    <head>
        <title>Lingovox Terms of Service</title>
        <meta charset="utf-8">
    </head>
    <body style="font-family:Arial;max-width:900px;margin:auto;padding:40px;">
        <h1>Terms of Service</h1>

        <p>Lingovox is an AI-powered Telegram bot that translates voice messages and returns translated voice responses.</p>

        <h2>Service Description</h2>
        <p>The service allows users to send voice messages which are automatically transcribed, translated into a selected language, and returned as voice audio.</p>

        <h2>Free Trial</h2>
        <p>New users may receive a limited number of free trial messages.</p>

        <h2>Paid Usage</h2>
        <p>After the free trial is used, users may purchase additional minutes inside the Telegram bot. Purchased minutes are consumed based on the duration of processed audio.</p>

        <h2>Payments</h2>
        <p>Payments are processed through third-party payment providers. Lingovox does not store payment card or cryptocurrency wallet details.</p>

        <h2>Refund Policy</h2>
        <p>Used minutes are non-refundable. If minutes were not credited due to a technical issue, users may contact support.</p>

        <h2>Service Availability</h2>
        <p>We strive to keep the service operational but cannot guarantee uninterrupted availability.</p>

        <h2>Support</h2>
        <p>Support is available inside the Telegram bot via the <b>/support</b> command.</p>

    </body>
    </html>
    """


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """
    <!doctype html>
    <html>
    <head>
        <title>Lingovox Privacy Policy</title>
        <meta charset="utf-8">
    </head>
    <body style="font-family:Arial;max-width:900px;margin:auto;padding:40px;">
        <h1>Privacy Policy</h1>

        <p>This privacy policy explains how Lingovox collects and uses information when users interact with the Telegram bot.</p>

        <h2>Information We Collect</h2>
        <p>We store limited information required to operate the service:</p>

        <ul>
            <li>Telegram user ID</li>
            <li>Selected language preferences</li>
            <li>Trial usage counters</li>
            <li>Purchased balance in minutes</li>
        </ul>

        <h2>Voice Processing</h2>
        <p>Voice messages are processed to generate translations and voice responses. Audio data may be processed by AI service providers.</p>

        <h2>Payments</h2>
        <p>Payments are handled by third-party payment providers. Lingovox does not store payment card details or cryptocurrency wallet information.</p>

        <h2>Data Protection</h2>
        <p>We take reasonable measures to protect user data and limit stored information to what is necessary for the service to function.</p>

        <h2>Third-Party Services</h2>
        <p>The service may use external APIs for speech recognition, translation, and voice synthesis.</p>

        <h2>Contact</h2>
        <p>If you have questions about this policy, please contact support inside the Telegram bot.</p>

    </body>
    </html>
    """
