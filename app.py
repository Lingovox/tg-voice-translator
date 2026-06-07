import os
import time
import json
import logging
import hashlib
import hmac
import re
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

import requests

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse

from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Boolean, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

# ==== Env ====
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini").strip()

NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY", "").strip()
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "").strip()

PADDLE_API_KEY = os.getenv("PADDLE_API_KEY", "").strip()
PADDLE_WEBHOOK_SECRET = os.getenv("PADDLE_WEBHOOK_SECRET", "").strip()
PADDLE_CLIENT_TOKEN = os.getenv("PADDLE_CLIENT_TOKEN", "").strip()
PADDLE_ENV = os.getenv("PADDLE_ENV", "live").strip().lower()

PADDLE_PRICE_30 = os.getenv("PADDLE_PRICE_30", "").strip()
PADDLE_PRICE_60 = os.getenv("PADDLE_PRICE_60", "").strip()
PADDLE_PRICE_180 = os.getenv("PADDLE_PRICE_180", "").strip()
PADDLE_PRICE_600 = os.getenv("PADDLE_PRICE_600", "").strip()

TRIAL_LIMIT = int(os.getenv("TRIAL_LIMIT", "5"))
TRIAL_MAX_SECONDS = int(os.getenv("TRIAL_MAX_SECONDS", "60"))
MIN_BILLABLE_SECONDS = int(os.getenv("MIN_BILLABLE_SECONDS", "1"))

# ==== Constants ====
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
NP_CREATE_INVOICE_URL = "https://api.nowpayments.io/v1/invoice"
POSTBACK_PATH = "/payments/nowpayments"
PADDLE_POSTBACK_PATH = "/payments/paddle"
PADDLE_API_BASE = "https://api.paddle.com" if PADDLE_ENV == "live" else "https://sandbox-api.paddle.com"

PADDLE_PRICES = {
    "P30": PADDLE_PRICE_30,
    "P60": PADDLE_PRICE_60,
    "P180": PADDLE_PRICE_180,
    "P600": PADDLE_PRICE_600,
}

PACKAGES = {
    "P30": {"usd": 10, "minutes": 30},
    "P60": {"usd": 15, "minutes": 60},
    "P180": {"usd": 30, "minutes": 180},
    "P600": {"usd": 70, "minutes": 600},
}

LANGS = [
    ("English", "en"), ("Русский", "ru"), ("O'zbek", "uz"),
    ("हिन्दी", "hi"), ("Español", "es"), ("ქართული", "ka"),
    ("العربية", "ar"), ("Português", "pt"), ("Türkçe", "tr"), ("Қазақша", "kk"),
]
LANG_LABELS = {code: name for name, code in LANGS}
SUPPORTED_LANG_CODES = {code for _, code in LANGS}

def lang_name(code: str) -> str:
    return LANG_LABELS.get((code or "").strip().lower(), (code or "").strip().lower() or "unknown")

# ==== DB ====
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
    conversation_source_lang = Column(String, nullable=True)
    conversation_target_lang = Column(String, nullable=True)
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
    provider = Column(String, nullable=False, default="nowpayments")
    external_id = Column(String, nullable=True, unique=True)
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
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS mode VARCHAR DEFAULT 'translate'")
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS conversation_source_lang VARCHAR")
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS conversation_target_lang VARCHAR")
        conn.exec_driver_sql("ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider VARCHAR DEFAULT 'nowpayments'")
        conn.exec_driver_sql("ALTER TABLE payments ADD COLUMN IF NOT EXISTS external_id VARCHAR")

# ==== Telegram helpers ====
def tg_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{TG_API}/{method}"
    r = requests.post(url, json=payload, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "raw": r.text, "status": r.status_code}

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

def build_main_keyboard(selected_lang: str, mode: str = "translate", conversation_ready: bool = False) -> Dict[str, Any]:
    rows = []
    mode_prefix = "✅ " if mode == "conversation" else ""
    rows.append([{"text": f"{mode_prefix}🗣 Conversation", "callback_data": "mode:conversation"}])
    for i in range(0, len(LANGS), 2):
        pair = LANGS[i:i + 2]
        row = []
        for title, code in pair:
            prefix = "✅ " if code == selected_lang and mode != "conversation" else ""
            row.append({"text": f"{prefix}{title}", "callback_data": f"lang:{code}"})
        rows.append(row)
    if conversation_ready:
        rows.append([{"text": "🔄 Reset conversation", "callback_data": "conversation:reset"}])
    rows.append([{"text": "💳 Buy minutes", "callback_data": "buy:menu"}])
    rows.append([{"text": "🆘 Support", "callback_data": "support:menu"}])
    return {"inline_keyboard": rows}

def build_packages_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "💳 Card — 30 min — $10", "callback_data": "paddle:P30"}],
            [{"text": "💳 Card — 60 min — $15", "callback_data": "paddle:P60"}],
            [{"text": "💳 Card — 180 min — $30", "callback_data": "paddle:P180"}],
            [{"text": "💳 Card — 600 min — $70", "callback_data": "paddle:P600"}],
            [{"text": "💎 Crypto — 30 min — $10", "callback_data": "buy:P30"}],
            [{"text": "💎 Crypto — 60 min — $15", "callback_data": "buy:P60"}],
            [{"text": "💎 Crypto — 180 min — $30", "callback_data": "buy:P180"}],
            [{"text": "💎 Crypto — 600 min — $70", "callback_data": "buy:P600"}],
            [{"text": "⬅️ Back", "callback_data": "buy:back"}],
        ]
    }

def format_status_text(user: User) -> str:
    bal_min = max(0, int(user.balance_seconds or 0)) // 60
    if (user.mode or "translate") == "conversation":
        if user.conversation_source_lang and user.conversation_target_lang:
            conversation_line = f"🗣 Conversation: {user.conversation_source_lang} ↔ {user.conversation_target_lang}\nSend voice messages from either side.\n"
        else:
            conversation_line = "🗣 Conversation mode is on\nStart with a phrase like: 'Translate to Spanish: hello, how are you?'\n"
        return f"🎙 Lingovox — AI live conversation\n\n{conversation_line}🎁 Free messages left: {user.trial_left} (≤ {TRIAL_MAX_SECONDS}s)\n💳 Balance: {bal_min} min\n\nBot remembers the pair of languages from your voice command and then translates in both directions."
    return f"🎙 Lingovox — AI voice translator\n\n🌍 Target language: {lang_name(user.target_lang)}\n🎁 Free messages left: {user.trial_left} (≤ {TRIAL_MAX_SECONDS}s)\n💳 Balance: {bal_min} min\n\nSend a voice message — I'll translate and reply with voice."

def ensure_user(db, chat_id: int) -> User:
    u = db.get(User, int(chat_id))
    if not u:
        u = User(telegram_id=int(chat_id), target_lang="en", trial_left=TRIAL_LIMIT, mode="translate")
        db.add(u)
        db.commit()
        db.refresh(u)
    return u

def user_keyboard(user: User) -> Dict[str, Any]:
    return build_main_keyboard(user.target_lang, mode=(user.mode or "translate"), conversation_ready=bool(user.conversation_source_lang and user.conversation_target_lang))

def decide_billing(user: User, voice_seconds: int) -> Tuple[str, int]:
    sec = max(MIN_BILLABLE_SECONDS, int(voice_seconds))
    if (user.trial_left or 0) > 0 and sec <= TRIAL_MAX_SECONDS:
        return "trial", 0
    if int(user.balance_seconds or 0) >= sec:
        return "paid", sec
    return "deny", 0

def apply_billing(db, user: User, mode: str, charge: int):
    if mode == "trial":
        user.trial_left = max(0, int(user.trial_left or 0) - 1)
    elif mode == "paid":
        user.balance_seconds = max(0, int(user.balance_seconds or 0) - charge)
    user.updated_at = datetime.utcnow()
    db.add(user)

# ==== OpenAI helpers ====
def _openai_headers() -> Dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing")
    return {"Authorization": f"Bearer {OPENAI_API_KEY}"}

def openai_transcribe_verbose(ogg_bytes: bytes) -> Dict[str, Any]:
    url = "https://api.openai.com/v1/audio/transcriptions"
    files = {"file": ("audio.ogg", ogg_bytes, "audio/ogg"), "model": (None, "whisper-1"), "response_format": (None, "json")}
    r = requests.post(url, headers=_openai_headers(), files=files, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Transcription failed: {r.text}")
    data = r.json()
    return {"text": str(data.get("text") or "").strip()}

def openai_tts(text: str) -> bytes:
    url = "https://api.openai.com/v1/audio/speech"
    p = {"model": "tts-1", "voice": "alloy", "format": "ogg", "input": text}
    r = requests.post(url, headers=_openai_headers(), json=p, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"TTS failed: {r.text}")
    return r.content

def openai_translate_text(text: str, target_lang: str, source_lang: Optional[str] = None) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = _openai_headers()
    headers["Content-Type"] = "application/json"
    sl = f"from {source_lang} " if source_lang else ""
    prompt = f"Translate the following text {sl}to {target_lang}. Return only the translation.\n\nText:\n{text}"
    payload = {"model": OPENAI_TEXT_MODEL, "messages": [{"role": "user", "content": prompt}]}
    r = requests.post(url, headers=headers, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Translation failed: {r.text}")
    return r.json()["choices"][0]["message"]["content"].strip()

# ==== Payment helpers ====
def credit_payment_if_needed(db, payment: Payment) -> bool:
    if (payment.status or "").lower() == "paid":
        return False
    pkg = PACKAGES.get(payment.package_code)
    if not pkg:
        return False
    user = ensure_user(db, int(payment.telegram_id))
    user.balance_seconds = max(0, int(user.balance_seconds or 0) + int(pkg["minutes"] * 60))
    user.updated_at = datetime.utcnow()
    payment.status = "paid"
    payment.updated_at = datetime.utcnow()
    db.add(user)
    db.add(payment)
    db.commit()
    db.refresh(user)
    tg_send_message(int(user.telegram_id), f"✅ Payment received!\nCredited: {pkg['minutes']} min", reply_markup=user_keyboard(user))
    return True

# ==== FastAPI App ====
app = FastAPI()

@app.on_event("startup")
def startup():
    init_db()

@app.get("/", response_class=HTMLResponse)
def landing():
    bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lingovox · AI Voice Translator</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:linear-gradient(135deg,#0a0f1e 0%,#0d1428 100%);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#eef2ff;line-height:1.5;min-height:100vh;padding:24px}}
.container{{max-width:1200px;margin:0 auto}}
.hero{{text-align:center;padding:60px 20px 40px}}
.badge{{display:inline-block;background:rgba(59,130,246,0.2);backdrop-filter:blur(4px);padding:6px 16px;border-radius:40px;font-size:0.85rem;font-weight:500;color:#60a5fa;margin-bottom:24px;border:1px solid rgba(59,130,246,0.3)}}
h1{{font-size:3.5rem;font-weight:800;background:linear-gradient(135deg,#fff,#94a3f8);-webkit-background-clip:text;background-clip:text;color:transparent;margin-bottom:20px}}
.subhead{{font-size:1.25rem;color:#9ca3af;max-width:600px;margin:0 auto 32px}}
.btn-group{{display:flex;gap:16px;justify-content:center;flex-wrap:wrap}}
.btn{{display:inline-flex;align-items:center;gap:10px;padding:14px 32px;border-radius:48px;font-weight:600;text-decoration:none;transition:all 0.2s;font-size:1rem}}
.btn-primary{{background:#3b82f6;color:#fff;box-shadow:0 4px 12px rgba(59,130,246,0.3)}}
.btn-primary:hover{{background:#2563eb;transform:scale(1.02)}}
.btn-outline{{background:transparent;border:1px solid #334155;color:#cbd5e1}}
.btn-outline:hover{{border-color:#3b82f6;color:#fff;background:rgba(59,130,246,0.1)}}
.features{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:28px;margin:60px 0;padding:0 16px}}
.feature-card{{background:rgba(17,24,39,0.6);backdrop-filter:blur(8px);border:1px solid #1f2a44;border-radius:28px;padding:28px 20px;transition:all 0.2s}}
.feature-card:hover{{border-color:#3b82f6;transform:translateY(-4px)}}
.feature-icon{{font-size:2.5rem;margin-bottom:18px}}
.feature-card h3{{font-size:1.35rem;font-weight:600;margin-bottom:12px;color:#f1f5f9}}
.feature-card p{{color:#94a3b8;font-size:0.95rem}}
.pricing{{background:rgba(0,0,0,0.2);border-radius:32px;padding:48px 24px;margin:40px 0;text-align:center}}
.pricing h2{{font-size:2rem;margin-bottom:16px}}
.price-grid{{display:flex;flex-wrap:wrap;justify-content:center;gap:20px;margin-top:32px}}
.price-card{{background:#0f172a;border-radius:24px;padding:24px 28px;min-width:160px;border:1px solid #2d3a5e}}
.price-card .minutes{{font-size:1.8rem;font-weight:700}}
.price-card .usd{{font-size:1.2rem;color:#fbbf24;margin:8px 0}}
.price-card .crypto{{font-size:0.8rem;color:#6b7280}}
.footer{{text-align:center;padding:48px 20px 24px;border-top:1px solid #1f2a44;margin-top:40px}}
.footer-links{{display:flex;justify-content:center;gap:32px;margin-bottom:24px;flex-wrap:wrap}}
.footer-links a{{color:#9ca3af;text-decoration:none;font-size:0.9rem}}
.footer-links a:hover{{color:#60a5fa}}
.copyright{{font-size:0.8rem;color:#4b5563}}
@media(max-width:640px){{h1{{font-size:2.2rem}}.hero{{padding:30px 16px}}.btn{{padding:10px 22px}}}}
</style>
</head>
<body>
<div class="container">
<div class="hero">
<div class="badge">✨ Real‑time voice translation</div>
<h1>Lingovox AI</h1>
<div class="subhead">Speak in your language — bot replies with voice in theirs.<br>Crypto & card payments. No subscriptions.</div>
<div class="btn-group">
<a href="{bot_link}" class="btn btn-primary">🎙️ Open in Telegram</a>
<a href="/terms" class="btn btn-outline">📜 Terms</a>
<a href="/privacy" class="btn btn-outline">🔒 Privacy</a>
</div>
</div>
<div class="features">
<div class="feature-card"><div class="feature-icon">🗣️</div><h3>Voice → Voice</h3><p>Send a voice message, get a translated voice reply instantly.</p></div>
<div class="feature-card"><div class="feature-icon">🔄</div><h3>Conversation Mode</h3><p>Two people speaking different languages can chat naturally.</p></div>
<div class="feature-card"><div class="feature-icon">💎</div><h3>Pay with Crypto</h3><p>USDT (TRC-20) or credit card — anonymous, fast.</p></div>
<div class="feature-card"><div class="feature-icon">🌍</div><h3>10+ Languages</h3><p>English, Русский, O'zbek, Hindi, Spanish, Arabic and more.</p></div>
<div class="feature-card"><div class="feature-icon">🎁</div><h3>Free trial</h3><p>5 messages (up to 60s each) — no credit card required.</p></div>
<div class="feature-card"><div class="feature-icon">⚡</div><h3>OpenAI powered</h3><p>Whisper + GPT + HD TTS — human-like voice.</p></div>
</div>
<div class="pricing">
<h2>💳 Simple pricing</h2>
<p>Pay once, use anytime. No expiry.</p>
<div class="price-grid">
<div class="price-card"><div class="minutes">30 min</div><div class="usd">$10</div><div class="crypto">≈ 10 USDT</div></div>
<div class="price-card"><div class="minutes">60 min</div><div class="usd">$15</div><div class="crypto">≈ 15 USDT</div></div>
<div class="price-card"><div class="minutes">180 min</div><div class="usd">$30</div><div class="crypto">≈ 30 USDT</div></div>
<div class="price-card"><div class="minutes">600 min</div><div class="usd">$70</div><div class="crypto">≈ 70 USDT</div></div>
</div>
<p style="margin-top:32px;font-size:0.85rem;color:#6b7280;">💳 Card via Paddle · 💎 Crypto via NowPayments (USDT TRC-20)</p>
</div>
<div class="footer">
<div class="footer-links"><a href="/terms">Terms of Service</a><a href="/privacy">Privacy Policy</a><a href="{bot_link}">Telegram Bot</a></div>
<div class="copyright">© 2025 Lingovox · AI voice translator</div>
</div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)

@app.get("/terms", response_class=HTMLResponse)
def terms_page():
    return HTMLResponse("""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Terms of Service</title><style>body{background:#0a0f1e;font-family:sans-serif;color:#e2e8f0;padding:48px 24px}.container{max-width:800px;margin:0 auto;background:rgba(17,24,39,0.8);border-radius:32px;padding:40px 32px}h1{color:#fff}.back-link{display:inline-block;margin-top:32px;padding:8px 20px;background:#1e293b;border-radius:40px;text-decoration:none;color:#60a5fa}</style></head><body><div class=\"container\"><h1>Terms of Service</h1><p>By using Lingovox Telegram bot, you agree to these terms.</p><h2>Free Trial & Payments</h2><p>5 free messages (max 60s each). Paid minutes never expire and are non-refundable.</p><h2>Acceptable Use</h2><p>Do not use for illegal or harmful purposes. Violation leads to ban without refund.</p><a href=\"/\" class=\"back-link\">← Back</a></div></body></html>""")

@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return HTMLResponse("""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Privacy Policy</title><style>body{background:#0a0f1e;font-family:sans-serif;color:#e2e8f0;padding:48px 24px}.container{max-width:800px;margin:0 auto;background:rgba(17,24,39,0.8);border-radius:32px;padding:40px 32px}.back-link{display:inline-block;margin-top:32px;padding:8px 20px;background:#1e293b;border-radius:40px;text-decoration:none;color:#60a5fa}</style></head><body><div class=\"container\"><h1>Privacy Policy</h1><p>We collect Telegram ID, language preferences, and voice messages (deleted after processing). Payments via Paddle/NowPayments — we don't store card/wallet details.</p><p>Voice messages are not permanently stored. You may request deletion via /support.</p><a href=\"/\" class=\"back-link\">← Back</a></div></body></html>""")

@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()
    try:
        if "message" in update:
            msg = update["message"]
            chat_id = msg.get("chat", {}).get("id")
            if not chat_id:
                return JSONResponse({"ok": True})
            text = (msg.get("text") or "").strip()
            
            if text == "/start":
                with SessionLocal() as db:
                    u = ensure_user(db, chat_id)
                    tg_send_message(chat_id, format_status_text(u), reply_markup=user_keyboard(u))
                return JSONResponse({"ok": True})
            
            if text == "/buy":
                tg_send_message(chat_id, "💳 Choose package:", reply_markup=build_packages_keyboard())
                return JSONResponse({"ok": True})
            
            if "voice" in msg:
                v = msg["voice"]
                fid, dur = v["file_id"], int(v.get("duration", 0))
                with SessionLocal() as db:
                    u = ensure_user(db, chat_id)
                    b_mode, ch = decide_billing(u, dur)
                    if b_mode == "deny":
                        tg_send_message(chat_id, "⛔ Not enough balance. Tap \"Buy minutes\".", reply_markup=user_keyboard(u))
                        return JSONResponse({"ok": True})
                    
                    gf = requests.get(f"{TG_API}/getFile", params={"file_id": fid}).json()
                    furl = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{gf['result']['file_path']}"
                    audio = requests.get(furl).content
                    
                    trans = openai_transcribe_verbose(audio)
                    otext = trans["text"]
                    if not otext:
                        raise RuntimeError("Empty voice")
                    
                    ttext = openai_translate_text(otext, lang_name(u.target_lang))
                    tts = openai_tts(ttext)
                    apply_billing(db, u, b_mode, ch)
                    db.commit()
                    
                    cap = f"🎁 Trial left: {u.trial_left}" if b_mode == "trial" else f"💳 Balance: {u.balance_seconds//60} min"
                    tg_send_voice(chat_id, tts, caption=cap)
                    tg_send_message(chat_id, format_status_text(u), reply_markup=user_keyboard(u))
                return JSONResponse({"ok": True})
        
        if "callback_query" in update:
            cq = update["callback_query"]
            data, chat_id, cq_id = cq["data"], cq["message"]["chat"]["id"], cq["id"]
            with SessionLocal() as db:
                u = ensure_user(db, chat_id)
                if data.startswith("lang:"):
                    u.target_lang = data.split(":")[1]
                    u.mode = "translate"
                    u.conversation_source_lang = u.conversation_target_lang = None
                    db.commit()
                    tg_send_message(chat_id, format_status_text(u), reply_markup=user_keyboard(u))
                elif data == "mode:conversation":
                    u.mode = "conversation"
                    u.conversation_source_lang = u.conversation_target_lang = None
                    db.commit()
                    tg_send_message(chat_id, "🗣 Conversation mode on.", reply_markup=user_keyboard(u))
                elif data == "conversation:reset":
                    u.conversation_source_lang = u.conversation_target_lang = None
                    db.commit()
                    tg_send_message(chat_id, "🔄 Reset.", reply_markup=user_keyboard(u))
                elif data == "buy:menu":
                    tg_send_message(chat_id, "💳 Choose package:", reply_markup=build_packages_keyboard())
                elif data == "support:menu":
                    tg_send_message(chat_id, "🆘 Use /support <message>")
            tg_answer_callback(cq_id)
            return JSONResponse({"ok": True})
    except Exception as e:
        log.exception("Webhook error")
    return JSONResponse({"ok": True})

@app.post(POSTBACK_PATH)
async def nowpayments_postback(req: Request):
    raw = await req.body()
    return PlainTextResponse("ok")
