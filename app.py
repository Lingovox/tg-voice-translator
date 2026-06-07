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

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean, DateTime
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import IntegrityError


# ====
# Logging
# ====
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")


# ====
# Env
# ====
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


# ====
# Constants
# ====
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
    ("English", "en"),
    ("Русский", "ru"),
    ("O'zbek", "uz"),
    ("हिन्दी", "hi"),
    ("Español", "es"),
    ("ქართული", "ka"),
    ("العربية", "ar"),
    ("Português", "pt"),
    ("Türkçe", "tr"),
    ("Қазақша", "kk"),
]

LANG_LABELS = {code: name for name, code in LANGS}
SUPPORTED_LANG_CODES = {code for _, code in LANGS}

LANG_ALIASES = {
    "en": ["english", "английский", "английском", "английскую", "ingliz", "инглиш"],
    "ru": ["russian", "русский", "русском", "русскую", "russkiy", "рус"],
    "uz": ["uzbek", "узбекский", "узбекском", "узбекскую", "o'zbek", "узб"],
    "hi": ["hindi", "хинди", "хинди", "хинди"],
    "es": ["spanish", "espanol", "español", "испанский", "испанском", "испанскую"],
    "ka": ["georgian", "грузинский", "грузинском", "грузинскую", "ქართული"],
    "ar": ["arabic", "арабский", "арабском", "арабскую", "العربية"],
    "pt": ["portuguese", "португальский", "португальском", "португальскую"],
    "tr": ["turkish", "турецкий", "турецком", "турецкую", "türkçe"],
    "kk": ["kazakh", "казахский", "казахском", "казахскую", "қазақша"],
}

_LANGUAGE_CANON_CACHE: Dict[str, str] = {}


def find_language_code_in_text(text: str) -> str:
    low = (text or "").strip().lower()
    if not low:
        return ""
    for code, aliases in LANG_ALIASES.items():
        for alias in aliases:
            if alias in low:
                return code
    return ""


def normalize_lang_code(value: str) -> str:
    raw = (value or "").strip().lower().replace("_", "-")
    if not raw:
        return ""
    base = raw.split("-", 1)[0]
    if base in SUPPORTED_LANG_CODES:
        return base
    return find_language_code_in_text(raw)


def resolve_source_language(text: str, detected_lang: str, telegram_lang: str, prefer_text: bool = False) -> str:
    detected = normalize_lang_code(detected_lang)
    detected_from_text = ""
    try:
        detected_from_text = normalize_lang_code(detect_language_from_text(text))
    except Exception:
        detected_from_text = ""
    if prefer_text:
        if detected_from_text:
            return detected_from_text
        if detected:
            return detected
    else:
        if detected:
            return detected
        if detected_from_text:
            return detected_from_text
    return normalize_lang_code(telegram_lang)


def decide_conversation_target(source_lang: str, target_lang: str, incoming_lang: str, telegram_lang: str) -> Tuple[str, str]:
    source_lang = normalize_lang_code(source_lang)
    target_lang = normalize_lang_code(target_lang)
    incoming_lang = normalize_lang_code(incoming_lang)
    telegram_lang = normalize_lang_code(telegram_lang)
    if incoming_lang == source_lang:
        return target_lang, incoming_lang
    if incoming_lang == target_lang:
        return source_lang, incoming_lang
    if not incoming_lang and telegram_lang == source_lang:
        return target_lang, telegram_lang
    if not incoming_lang and telegram_lang == target_lang:
        return source_lang, telegram_lang
    if incoming_lang and incoming_lang not in {source_lang, target_lang}:
        if telegram_lang == source_lang:
            return target_lang, source_lang
        if telegram_lang == target_lang:
            return source_lang, target_lang
        raise RuntimeError(
            f"Detected language '{lang_name(incoming_lang)}' is outside this conversation: "
            f"{lang_name(source_lang)} ↔ {lang_name(target_lang)}"
        )
    if telegram_lang == source_lang:
        return target_lang, source_lang
    if telegram_lang == target_lang:
        return source_lang, target_lang
    raise RuntimeError(
        f"Could not determine translation direction for conversation: "
        f"{lang_name(source_lang)} ↔ {lang_name(target_lang)}"
    )


def parse_conversation_setup_local(text: str) -> Dict[str, str]:
    original = (text or "").strip()
    low = original.lower()
    has_setup_command = any(cmd in low for cmd in [
        "translate to", "translate into", "переведи на", "перевести на",
        "übersetze auf", "übersetze ins", "übersetze in", "translate", "переведи", "перевести"
    ])
    target_lang = ""
    message_text = original
    patterns = [
        r"^\s*переведи\s+на\s+(.+?)(?:\s+язык)?[,:;\.\!\?\-]\s*(.+)$",
        r"^\s*перевести\s+на\s+(.+?)(?:\s+язык)?[,:;\.\!\?\-]\s*(.+)$",
        r"^\s*translate\s+(?:to|into)\s+(.+?)[,:;\.\!\?\-]\s*(.+)$",
        r"^\s*übersetze\s+(?:auf|ins?|in)\s+(.+?)[,:;\.\!\?\-]\s*(.+)$",
    ]
    for pattern in patterns:
        m = re.match(pattern, original, flags=re.IGNORECASE)
        if m:
            target_lang = m.group(1).strip().strip(" -–—")
            message_text = m.group(2).strip()
            break
    return {
        "target_lang": target_lang,
        "message_text": (message_text or "").strip(),
        "has_setup_command": has_setup_command,
    }


def normalize_language_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def detect_language_name_from_text(text: str) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = _openai_headers()
    headers["Content-Type"] = "application/json"
    prompt = (
        "Detect the language of the text. "
        "Return only JSON like {\"language\":\"Russian\"}. "
        "Use a plain English language name, not a code.\n\n"
        f"Text:\n{text}"
    )
    payload = {
        "model": OPENAI_TEXT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI language detect failed: HTTP {r.status_code}")
    out = r.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(out)
    except Exception:
        parsed = {}
    language = str(parsed.get("language") or "").strip()
    if not language:
        raise RuntimeError("Could not detect source language")
    return language


def parse_conversation_setup(text: str) -> Dict[str, str]:
    local = parse_conversation_setup_local(text)
    if local.get("target_lang") and local.get("message_text") and local.get("has_setup_command"):
        return local
    url = "https://api.openai.com/v1/chat/completions"
    headers = _openai_headers()
    headers["Content-Type"] = "application/json"
    prompt = (
        "Extract conversation setup from the user's first phrase for a voice translation bot. "
        "The user may say things like 'translate to Spanish: hello', 'переведи на таджикский язык. привет', "
        "or similar phrases in any language. "
        "Return only JSON with keys target_lang, message_text, has_setup_command. "
        "target_lang must be a plain English language name like Spanish, Tajik, Uzbek, Kazakh, Arabic, Japanese, etc. "
        "Do not return a language code. "
        "If the target language is unclear, return target_lang as an empty string. "
        "message_text must contain only the part that should actually be translated, without the command.\n\n"
        f"Text:\n{text}"
    )
    payload = {
        "model": OPENAI_TEXT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI conversation setup parse failed: HTTP {r.status_code}")
    out = r.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(out)
    except Exception:
        parsed = {}
    target_lang = str(parsed.get("target_lang") or "").strip()
    message_text = str(parsed.get("message_text") or "").strip()
    has_setup_command = bool(parsed.get("has_setup_command"))
    if not target_lang:
        target_lang = local.get("target_lang", "")
    if not message_text:
        message_text = local.get("message_text", "")
    return {
        "target_lang": target_lang,
        "message_text": message_text,
        "has_setup_command": has_setup_command or bool(local.get("has_setup_command")),
    }


def canonicalize_language_name(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    key = normalize_language_name(raw)
    cached = _LANGUAGE_CANON_CACHE.get(key)
    if cached:
        return cached
    url = "https://api.openai.com/v1/chat/completions"
    headers = _openai_headers()
    headers["Content-Type"] = "application/json"
    prompt = (
        "Normalize the following language reference to a canonical plain English language name. "
        "Examples: 'таджикский' -> 'Tajik', 'Тоҷикӣ' -> 'Tajik', 'русский' -> 'Russian', 'espanol' -> 'Spanish'. "
        "Return only JSON like {\"language\":\"Tajik\"}.\n\n"
        f"Language reference:\n{raw}"
    )
    payload = {
        "model": OPENAI_TEXT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI language normalization failed: HTTP {r.status_code}")
    out = r.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(out)
    except Exception:
        parsed = {}
    language = str(parsed.get("language") or "").strip()
    if not language:
        language = raw
    _LANGUAGE_CANON_CACHE[key] = language
    return language


def choose_language_in_pair(source_lang: str, target_lang: str, incoming_lang: str) -> str:
    source_canonical = canonicalize_language_name(source_lang)
    target_canonical = canonicalize_language_name(target_lang)
    incoming_canonical = canonicalize_language_name(incoming_lang)
    src = normalize_language_name(source_canonical)
    tgt = normalize_language_name(target_canonical)
    inc = normalize_language_name(incoming_canonical)
    if inc == src:
        return source_canonical
    if inc == tgt:
        return target_canonical
    url = "https://api.openai.com/v1/chat/completions"
    headers = _openai_headers()
    headers["Content-Type"] = "application/json"
    prompt = (
        "You are matching a detected language to one side of an active two-language conversation. "
        "Return only JSON like {\"match\":\"source\"} or {\"match\":\"target\"} or {\"match\":\"unknown\"}.\n\n"
        f"Source language: {source_canonical}\n"
        f"Target language: {target_canonical}\n"
        f"Detected incoming language: {incoming_canonical}\n"
    )
    payload = {
        "model": OPENAI_TEXT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI conversation match failed: HTTP {r.status_code}")
    out = r.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(out)
    except Exception:
        parsed = {}
    match = str(parsed.get("match") or "").strip().lower()
    if match == "source":
        return source_canonical
    if match == "target":
        return target_canonical
    raise RuntimeError(
        f"Detected language '{incoming_canonical}' is outside this conversation: "
        f"{source_canonical} ↔ {target_canonical}"
    )


def decide_conversation_target_any(source_lang: str, target_lang: str, incoming_lang: str) -> Tuple[str, str]:
    source_canonical = canonicalize_language_name(source_lang)
    target_canonical = canonicalize_language_name(target_lang)
    matched_side = choose_language_in_pair(source_canonical, target_canonical, incoming_lang)
    if normalize_language_name(matched_side) == normalize_language_name(source_canonical):
        return target_canonical, source_canonical
    return source_canonical, target_canonical


def lang_name(code: str) -> str:
    return LANG_LABELS.get((code or "").strip().lower(), (code or "").strip().lower() or "unknown")


# ====
# DB
# ====
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


# ====
# Telegram helpers
# ====
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
            conversation_line = (
                f"🗣 Conversation: {user.conversation_source_lang} ↔ {user.conversation_target_lang}\n"
                "Send voice messages from either side.\n"
            )
        else:
            conversation_line = (
                "🗣 Conversation mode is on\n"
                "Start with a phrase like: 'Translate to Spanish: hello, how are you?'\n"
            )
        return (
            "🎙 Lingovox — AI live conversation\n\n"
            f"{conversation_line}"
            f"🎁 Free messages left: {user.trial_left} (≤ {TRIAL_MAX_SECONDS}s)\n"
            f"💳 Balance: {bal_min} min\n\n"
            "Bot remembers the pair of languages from your voice command and then translates in both directions."
        )
    return (
        "🎙 Lingovox — AI voice translator\n\n"
        f"🌍 Target language: {lang_name(user.target_lang)}\n"
        f"🎁 Free messages left: {user.trial_left} (≤ {TRIAL_MAX_SECONDS}s)\n"
        f"💳 Balance: {bal_min} min\n\n"
        "Send a voice message — I'll translate and reply with voice."
    )


# ====
# NOWPayments helpers
# ====
def env_missing() -> list:
    missing = []
    if not NOWPAYMENTS_API_KEY: missing.append("NOWPAYMENTS_API_KEY")
    if not NOWPAYMENTS_IPN_SECRET: missing.append("NOWPAYMENTS_IPN_SECRET")
    if not BASE_URL: missing.append("BASE_URL")
    return missing


def nowpayments_create_invoice(order_id: str, amount_usd: int, description: str) -> Dict[str, Any]:
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}
    payload = {
        "price_amount": amount_usd, "price_currency": "usd", "pay_currency": "usdttrc20",
        "order_id": order_id, "order_description": description,
        "ipn_callback_url": f"{BASE_URL}{POSTBACK_PATH}",
        "success_url": f"{BASE_URL}/", "cancel_url": f"{BASE_URL}/",
        "is_fixed_rate": False, "is_fee_paid_by_user": False,
    }
    r = requests.post(NP_CREATE_INVOICE_URL, headers=headers, json=payload, timeout=30)
    if "application/json" not in (r.headers.get("content-type") or "").lower():
        return {"ok": False, "status": r.status_code, "raw": r.text}
    return {"ok": r.status_code in (200, 201), "status": r.status_code, "data": r.json()}


def verify_nowpayments_signature(raw_body: bytes, signature: str) -> bool:
    if not NOWPAYMENTS_IPN_SECRET or not signature: return False
    calc = hmac.new(NOWPAYMENTS_IPN_SECRET.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(calc, signature)


def paddle_env_missing() -> list:
    missing = []
    if not PADDLE_API_KEY: missing.append("PADDLE_API_KEY")
    if not PADDLE_WEBHOOK_SECRET: missing.append("PADDLE_WEBHOOK_SECRET")
    if not BASE_URL: missing.append("BASE_URL")
    return missing


def paddle_headers() -> Dict[str, str]:
    if not PADDLE_API_KEY: raise RuntimeError("PADDLE_API_KEY missing")
    return {"Authorization": f"Bearer {PADDLE_API_KEY}", "Content-Type": "application/json"}


def verify_paddle_signature(raw_body: bytes, sig_header: str, tolerance: int = 300) -> bool:
    if not PADDLE_WEBHOOK_SECRET or not sig_header: return False
    pairs = {}
    for pt in sig_header.split(";"):
        if "=" in pt:
            k, v = pt.split("=", 1)
            pairs.setdefault(k.strip(), []).append(v.strip())
    ts = (pairs.get("ts") or [None])[0]
    sigs = pairs.get("h1") or []
    if not ts or not sigs: return False
    try:
        if abs(time.time() - int(ts)) > tolerance: return False
    except: return False
    signed = ts.encode("utf-8") + b":" + raw_body
    expected = hmac.new(PADDLE_WEBHOOK_SECRET.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def paddle_create_transaction(package_code: str, telegram_id: int) -> Dict[str, Any]:
    price_id = (PADDLE_PRICES.get(package_code) or "").strip()
    if not price_id: raise RuntimeError(f"Paddle price not set for {package_code}")
    oid = f"pdl_{telegram_id}_{package_code}_{int(time.time())}"
    payload = {
        "items": [{"price_id": price_id, "quantity": 1}],
        "custom_data": {"telegram_id": str(telegram_id), "package_code": package_code, "order_id": oid},
        "checkout": {"url": f"{BASE_URL}/pay", "success_url": f"{BASE_URL}/?paid=1"},
    }
    r = requests.post(f"{PADDLE_API_BASE}/transactions", headers=paddle_headers(), json=payload, timeout=30)
    if "application/json" not in (r.headers.get("content-type") or "").lower():
        return {"ok": False, "status": r.status_code, "raw": r.text}
    return {"ok": r.status_code in (200, 201), "status": r.status_code, "data": r.json(), "order_id": oid}


def credit_payment_if_needed(db, payment: Payment) -> bool:
    if (payment.status or "").lower() == "paid": return False
    pkg = PACKAGES.get(payment.package_code)
    if not pkg: return False
    user = ensure_user(db, int(payment.telegram_id))
    user.balance_seconds = max(0, int(user.balance_seconds or 0) + int(pkg["minutes"] * 60))
    user.updated_at = datetime.utcnow()
    payment.status = "paid"
    payment.updated_at = datetime.utcnow()
    db.add(user); db.add(payment); db.commit(); db.refresh(user)
    tg_send_message(int(user.telegram_id), f"✅ Payment received!\nCredited: {pkg['minutes']} min", reply_markup=user_keyboard(user))
    return True


# ====
# OpenAI helpers
# ====
def _openai_headers() -> Dict[str, str]:
    if not OPENAI_API_KEY: raise RuntimeError("OPENAI_API_KEY missing")
    return {"Authorization": f"Bearer {OPENAI_API_KEY}"}


def openai_transcribe_verbose(ogg_bytes: bytes) -> Dict[str, Any]:
    url = "https://api.openai.com/v1/audio/transcriptions"
    files = {"file": ("audio.ogg", ogg_bytes, "audio/ogg"), "model": (None, "whisper-1"), "response_format": (None, "json")}
    r = requests.post(url, headers=_openai_headers(), files=files, timeout=90)
    if r.status_code != 200: raise RuntimeError(f"Transcription failed: {r.text}")
    data = r.json()
    return {"text": str(data.get("text") or "").strip()}


def openai_tts(text: str) -> bytes:
    url = "https://api.openai.com/v1/audio/speech"
    p = {"model": "tts-1", "voice": "alloy", "format": "ogg", "input": text}
    r = requests.post(url, headers=_openai_headers(), json=p, timeout=90)
    if r.status_code != 200: raise RuntimeError(f"TTS failed: {r.text}")
    return r.content


def openai_translate_text(text: str, target_lang: str, source_lang: Optional[str] = None) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = _openai_headers()
    headers["Content-Type"] = "application/json"
    sl = f"from {source_lang} " if source_lang else ""
    prompt = f"Translate the following text {sl}to {target_lang}. Return only the translation.\n\nText:\n{text}"
    payload = {"model": OPENAI_TEXT_MODEL, "messages": [{"role": "user", "content": prompt}]}
    r = requests.post(url, headers=headers, json=payload, timeout=90)
    if r.status_code != 200: raise RuntimeError(f"Translation failed: {r.text}")
    return r.json()["choices"][0]["message"]["content"].strip()


def detect_language_from_text(text: str) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = _openai_headers()
    headers["Content-Type"] = "application/json"
    codes = ", ".join(sorted(SUPPORTED_LANG_CODES))
    prompt = f"Detect language. Return only JSON like {{\"language\":\"ru\"}} from this list: {codes}.\n\nText:\n{text}"
    payload = {
        "model": OPENAI_TEXT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Detect failed: {r.text}")
    out = r.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(out)
        language
