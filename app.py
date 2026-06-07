import os
import time
import json
import logging
import hashlib
import hmac
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, List

import requests

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean, DateTime, func
)
from sqlalchemy.orm import sessionmaker, declarative_base


# =========================================================
# Logging
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lingovox")


# =========================================================
# Env
# =========================================================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini").strip()
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "alloy").strip()

# ---- Payments (только карта через Paddle) ----
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

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "90"))

# Смещение часового пояса для границы "сегодня" в статистике (Ташкент = +5)
STAT_TZ_OFFSET = int(os.getenv("STAT_TZ_OFFSET", "5"))


# =========================================================
# Startup validation
# =========================================================
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"

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


# =========================================================
# Languages  (НЕ ТРОГАЕМ)
# =========================================================
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


def lang_name(code: str) -> str:
    return LANG_LABELS.get((code or "").strip().lower(), (code or "").strip() or "unknown")


def find_language_code_in_text(text: str) -> str:
    low = (text or "").strip().lower()
    if not low:
        return ""
    for code, aliases in LANG_ALIASES.items():
        for alias in aliases:
            if alias and alias in low:
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


# =========================================================
# DB
# =========================================================
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
    # В режиме conversation храним коды языков из списка LANGS
    conversation_source_lang = Column(String, nullable=True)
    conversation_target_lang = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    order_id = Column(String, nullable=False, unique=True, index=True)
    invoice_id = Column(String, nullable=False, default="")
    package_code = Column(String, nullable=False)
    amount_usd = Column(Integer, nullable=False)
    provider = Column(String, nullable=False, default="paddle")
    external_id = Column(String, nullable=True)
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


class UsageEvent(Base):
    """Одна строка на каждый успешный перевод — для статистики использования."""
    __tablename__ = "usage_events"
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    mode = Column(String, nullable=False, default="translate")  # translate | conversation
    billing = Column(String, nullable=False, default="trial")   # trial | paid
    seconds = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


def init_db():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS mode VARCHAR DEFAULT 'translate'")
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS conversation_source_lang VARCHAR")
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS conversation_target_lang VARCHAR")
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_messages INTEGER DEFAULT 0")
        conn.exec_driver_sql("ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider VARCHAR DEFAULT 'paddle'")
        conn.exec_driver_sql("ALTER TABLE payments ADD COLUMN IF NOT EXISTS external_id VARCHAR")


# =========================================================
# Telegram helpers
# =========================================================
def tg_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        r = requests.post(f"{TG_API}/{method}", json=payload, timeout=30)
        return r.json()
    except Exception as e:
        log.warning("Telegram %s failed: %s", method, e)
        return {"ok": False, "error": str(e)}


def tg_send_message(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_request("sendMessage", payload)


def tg_send_voice(chat_id: int, voice_bytes: bytes, caption: Optional[str] = None) -> Dict[str, Any]:
    files = {"voice": ("voice.ogg", voice_bytes, "audio/ogg")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    try:
        r = requests.post(f"{TG_API}/sendVoice", data=data, files=files, timeout=120)
        return r.json()
    except Exception as e:
        log.warning("sendVoice failed: %s", e)
        return {"ok": False, "error": str(e)}


def tg_answer_callback(callback_query_id: str, text: Optional[str] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return tg_request("answerCallbackQuery", payload)


def tg_download_voice(file_id: str) -> bytes:
    gf = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30).json()
    if not gf.get("ok"):
        raise RuntimeError("Could not resolve voice file")
    file_path = gf["result"]["file_path"]
    audio = requests.get(f"{TG_FILE_API}/{file_path}", timeout=60)
    audio.raise_for_status()
    return audio.content


# =========================================================
# Keyboards
# =========================================================
def build_main_keyboard(selected_lang: str, mode: str = "translate", conversation_ready: bool = False) -> Dict[str, Any]:
    rows: List[List[Dict[str, str]]] = []
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
            [{"text": "⬅️ Back", "callback_data": "buy:back"}],
        ]
    }


def user_keyboard(user: "User") -> Dict[str, Any]:
    return build_main_keyboard(
        user.target_lang,
        mode=(user.mode or "translate"),
        conversation_ready=bool(user.conversation_source_lang and user.conversation_target_lang),
    )


def format_status_text(user: "User") -> str:
    bal_min = max(0, int(user.balance_seconds or 0)) // 60
    if (user.mode or "translate") == "conversation":
        if user.conversation_source_lang and user.conversation_target_lang:
            conversation_line = (
                f"🗣 Conversation: {user.conversation_source_lang} ↔ "
                f"{user.conversation_target_lang}\n"
                "Говорите с любой стороны — определю язык и переведу.\n"
            )
        else:
            conversation_line = (
                "🗣 Conversation mode is on\n"
                "Скажите голосом фразу-команду, например:\n"
                "«переведи на японский, как тебя зовут»\n"
                "Можно любой язык мира. Я запомню пару и буду переводить в обе стороны.\n"
            )
        return (
            "🎙 Lingovox — AI live conversation\n\n"
            f"{conversation_line}"
            f"🎁 Free messages left: {user.trial_left} (≤ {TRIAL_MAX_SECONDS}s)\n"
            f"💳 Balance: {bal_min} min\n\n"
            "I remember the language pair from your command and translate in both directions."
        )
    return (
        "🎙 Lingovox — AI voice translator\n\n"
        f"🌍 Target language: {lang_name(user.target_lang)}\n"
        f"🎁 Free messages left: {user.trial_left} (≤ {TRIAL_MAX_SECONDS}s)\n"
        f"💳 Balance: {bal_min} min\n\n"
        "Send a voice message — I'll translate it and reply with voice."
    )


# =========================================================
# OpenAI helpers
# =========================================================
def _openai_headers() -> Dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing")
    return {"Authorization": f"Bearer {OPENAI_API_KEY}"}


def _openai_post(url: str, *, json_body: Dict[str, Any], timeout: int = REQUEST_TIMEOUT) -> requests.Response:
    headers = _openai_headers()
    headers["Content-Type"] = "application/json"
    last_err = None
    for attempt in range(2):
        try:
            r = requests.post(url, headers=headers, json=json_body, timeout=timeout)
            if r.status_code == 200:
                return r
            last_err = f"HTTP {r.status_code}: {r.text[:300]}"
        except Exception as e:
            last_err = str(e)
        time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"OpenAI request failed: {last_err}")


def openai_transcribe(ogg_bytes: bytes) -> str:
    url = "https://api.openai.com/v1/audio/transcriptions"
    files = {
        "file": ("audio.ogg", ogg_bytes, "audio/ogg"),
        "model": (None, "whisper-1"),
        "response_format": (None, "json"),
    }
    r = requests.post(url, headers=_openai_headers(), files=files, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Transcription failed: {r.text[:300]}")
    return str(r.json().get("text") or "").strip()


def openai_tts(text: str) -> bytes:
    url = "https://api.openai.com/v1/audio/speech"
    # Telegram voice ждёт OGG/OPUS. У OpenAI параметр называется response_format,
    # а не format — иначе вернётся mp3 и звук будет "кашей".
    body = {
        "model": "tts-1",
        "voice": OPENAI_TTS_VOICE,
        "response_format": "opus",
        "input": text,
    }
    r = _openai_post(url, json_body=body, timeout=120)
    return r.content


def openai_translate_text(text: str, target_lang: str, source_lang: Optional[str] = None) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    src = f"from {source_lang} " if source_lang else ""
    prompt = (
        f"Translate the following text {src}to {target_lang}. "
        "Keep the tone natural and conversational. Return ONLY the translation, no notes.\n\n"
        f"Text:\n{text}"
    )
    body = {"model": OPENAI_TEXT_MODEL, "messages": [{"role": "user", "content": prompt}]}
    r = _openai_post(url, json_body=body)
    return r.json()["choices"][0]["message"]["content"].strip()


def gpt_parse_setup(text: str) -> Dict[str, str]:
    """
    Разбор команды настройки через GPT (не зависит от пунктуации Whisper).
    Язык НЕ ограничен кнопками — допускается ЛЮБОЙ язык (Japanese, Thai, ...).
    Возвращает {'target_lang': <англ. название или ''>, 'message_text': <что переводить>}.
    """
    url = "https://api.openai.com/v1/chat/completions"
    prompt = (
        "The user is configuring a voice translator. The phrase contains an instruction like "
        "'translate to <language>' / 'переведи на <язык>' followed by the actual text to translate. "
        "Speech-to-text may have NO punctuation, so split by meaning. "
        "The target language can be ANY world language, not a fixed list.\n"
        "Return ONLY JSON: {\"target\":\"<language name in English>\",\"message\":\"<text to translate>\"}. "
        "target is the plain English name of the language (e.g. Japanese, Thai, Spanish), or empty if unclear. "
        "message must be ONLY the part to translate, without the command words.\n\n"
        f"Phrase:\n{text}"
    )
    body = {
        "model": OPENAI_TEXT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    try:
        r = _openai_post(url, json_body=body, timeout=60)
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        return {
            "target_lang": str(out.get("target") or "").strip(),
            "message_text": str(out.get("message") or "").strip(),
        }
    except Exception as e:
        log.warning("gpt_parse_setup failed: %s", e)
        return {"target_lang": "", "message_text": ""}


def detect_language_name(text: str) -> str:
    """Определяет язык текста и возвращает его английское название (любой язык)."""
    url = "https://api.openai.com/v1/chat/completions"
    prompt = (
        "Detect the language of the text. Return ONLY JSON like {\"language\":\"Japanese\"} "
        "using the plain English name of the language (any world language).\n\nText:\n" + text
    )
    body = {
        "model": OPENAI_TEXT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    r = _openai_post(url, json_body=body, timeout=60)
    try:
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        return str(out.get("language") or "").strip()
    except Exception:
        return ""


def detect_language_code(text: str) -> str:
    """Определяет язык текста и возвращает код из SUPPORTED_LANG_CODES (или '')."""
    url = "https://api.openai.com/v1/chat/completions"
    codes = ", ".join(sorted(SUPPORTED_LANG_CODES))
    prompt = (
        f"Detect the language of the text. Return ONLY JSON like {{\"language\":\"ru\"}} "
        f"using one of these codes: {codes}. If none fit, use the closest.\n\nText:\n{text}"
    )
    body = {
        "model": OPENAI_TEXT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    r = _openai_post(url, json_body=body, timeout=60)
    try:
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        return normalize_lang_code(str(out.get("language") or ""))
    except Exception:
        return ""


# =========================================================
# Conversation setup parsing
# =========================================================
SETUP_PATTERNS = [
    r"^\s*переведи(?:те)?\s+на\s+(.+?)(?:\s+язык\w*)?\s*[,:;\.\!\?\-—–]\s*(.+)$",
    r"^\s*перевести\s+на\s+(.+?)(?:\s+язык\w*)?\s*[,:;\.\!\?\-—–]\s*(.+)$",
    r"^\s*translate\s+(?:to|into)\s+(.+?)\s*[,:;\.\!\?\-—–]\s*(.+)$",
    r"^\s*übersetze\s+(?:auf|ins?|in)\s+(.+?)\s*[,:;\.\!\?\-—–]\s*(.+)$",
]

SETUP_TRIGGERS = [
    "translate to", "translate into",
    "переведи на", "переведите на", "перевести на",
    "übersetze auf", "übersetze ins", "übersetze in",
]


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def parse_conversation_setup(text: str) -> Dict[str, Any]:
    """
    Распознаёт команду вида 'переведи на японский: как тебя зовут'.
    Работает с ЛЮБЫМ языком и даже без пунктуации (через GPT-fallback).
    Возвращает target_lang (англ. название языка или ''), message_text, has_command.
    """
    original = (text or "").strip()
    low = original.lower()
    has_command = any(c in low for c in SETUP_TRIGGERS)

    target_raw = ""
    message_text = original
    for pattern in SETUP_PATTERNS:
        m = re.match(pattern, original, flags=re.IGNORECASE)
        if m:
            target_raw = m.group(1).strip(" -–—")
            message_text = m.group(2).strip()
            break

    target_lang = ""
    # Если есть слова-триггеры — всегда уточняем у GPT (он канонизирует язык в
    # английское название и корректно отделит текст даже без пунктуации).
    if has_command:
        gpt = gpt_parse_setup(original)
        target_lang = gpt["target_lang"] or target_raw
        if gpt["message_text"]:
            message_text = gpt["message_text"]
    elif target_raw:
        target_lang = target_raw

    return {
        "target_lang": target_lang,
        "message_text": message_text,
        "has_command": has_command,
    }


def resolve_conversation_direction(source_lang: str, target_lang: str, incoming_lang: str) -> Tuple[str, str]:
    """
    По входящему языку решает, на какой язык переводить (по именам языков).
    Возвращает (translate_to, detected_from).
    """
    src = _norm_name(source_lang)
    tgt = _norm_name(target_lang)
    inc = _norm_name(incoming_lang)

    if inc and inc == tgt:
        return source_lang, target_lang
    if inc and inc == src:
        return target_lang, source_lang
    # Не удалось точно сопоставить — считаем, что говорят на source-стороне
    if incoming_lang:
        return target_lang, incoming_lang
    return target_lang, source_lang


# =========================================================
# Payments — Paddle (card only)
# =========================================================
def paddle_env_missing() -> list:
    missing = []
    if not PADDLE_API_KEY:
        missing.append("PADDLE_API_KEY")
    if not PADDLE_WEBHOOK_SECRET:
        missing.append("PADDLE_WEBHOOK_SECRET")
    if not BASE_URL:
        missing.append("BASE_URL")
    return missing


def paddle_headers() -> Dict[str, str]:
    if not PADDLE_API_KEY:
        raise RuntimeError("PADDLE_API_KEY missing")
    return {"Authorization": f"Bearer {PADDLE_API_KEY}", "Content-Type": "application/json"}


def verify_paddle_signature(raw_body: bytes, sig_header: str, tolerance: int = 300) -> bool:
    if not PADDLE_WEBHOOK_SECRET or not sig_header:
        return False
    pairs: Dict[str, list] = {}
    for pt in sig_header.split(";"):
        if "=" in pt:
            k, v = pt.split("=", 1)
            pairs.setdefault(k.strip(), []).append(v.strip())
    ts = (pairs.get("ts") or [None])[0]
    sigs = pairs.get("h1") or []
    if not ts or not sigs:
        return False
    try:
        if abs(time.time() - int(ts)) > tolerance:
            return False
    except Exception:
        return False
    signed = ts.encode("utf-8") + b":" + raw_body
    expected = hmac.new(PADDLE_WEBHOOK_SECRET.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def paddle_create_transaction(package_code: str, telegram_id: int) -> Dict[str, Any]:
    price_id = (PADDLE_PRICES.get(package_code) or "").strip()
    if not price_id:
        raise RuntimeError(f"Paddle price not set for {package_code}")
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


def credit_payment_if_needed(db, payment: "Payment") -> bool:
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
    tg_send_message(
        int(user.telegram_id),
        f"✅ Payment received!\nCredited: {pkg['minutes']} min",
        reply_markup=user_keyboard(user),
    )
    return True


# =========================================================
# Payment callback handlers (создание инвойсов)
# =========================================================
def handle_card_purchase(db, chat_id: int, package_code: str) -> None:
    pkg = PACKAGES.get(package_code)
    if not pkg:
        tg_send_message(chat_id, "⚠️ Unknown package.")
        return
    missing = paddle_env_missing()
    if missing:
        tg_send_message(chat_id, f"⚠️ Card payments are not configured: {', '.join(missing)}")
        return
    try:
        res = paddle_create_transaction(package_code, chat_id)
    except Exception as e:
        log.warning("Paddle transaction error: %s", e)
        tg_send_message(chat_id, "⚠️ Could not create card checkout. Try again later.")
        return
    if not res.get("ok"):
        log.warning("Paddle transaction failed: %s", res)
        tg_send_message(chat_id, "⚠️ Could not create card checkout. Try again later.")
        return
    tx = (res.get("data") or {}).get("data") or {}
    tx_id = str(tx.get("id") or "")
    checkout = (tx.get("checkout") or {}).get("url") or ""
    p = Payment(
        telegram_id=chat_id, order_id=res.get("order_id", f"pdl_{chat_id}_{int(time.time())}"),
        invoice_id=tx_id, package_code=package_code, amount_usd=int(pkg["usd"]),
        provider="paddle", external_id=tx_id, status="created",
    )
    db.add(p)
    db.commit()
    if checkout:
        tg_send_message(chat_id, f"💳 Pay with card:\n{checkout}")
    else:
        tg_send_message(chat_id, "⚠️ Checkout created but no link returned.")


# =========================================================
# Logic helpers
# =========================================================
def ensure_user(db, chat_id: int) -> "User":
    u = db.get(User, int(chat_id))
    if not u:
        u = User(telegram_id=int(chat_id), target_lang="en", trial_left=TRIAL_LIMIT, mode="translate")
        db.add(u)
        db.commit()
        db.refresh(u)
    return u


def decide_billing(user: "User", voice_seconds: int) -> Tuple[str, int]:
    sec = max(MIN_BILLABLE_SECONDS, int(voice_seconds))
    if (user.trial_left or 0) > 0 and sec <= TRIAL_MAX_SECONDS:
        return "trial", 0
    if int(user.balance_seconds or 0) >= sec:
        return "paid", sec
    return "deny", 0


def apply_billing(db, user: "User", mode: str, charge: int) -> None:
    if mode == "trial":
        user.trial_left = max(0, int(user.trial_left or 0) - 1)
    elif mode == "paid":
        user.balance_seconds = max(0, int(user.balance_seconds or 0) - charge)
    user.updated_at = datetime.utcnow()
    db.add(user)


def is_admin(chat_id: int) -> bool:
    return ADMIN_ID != "" and str(chat_id) == str(ADMIN_ID)


def admin_notify(text: str) -> None:
    if ADMIN_ID:
        try:
            tg_send_message(int(ADMIN_ID), text)
        except Exception:
            pass


def _today_utc_bounds() -> Tuple[datetime, datetime]:
    """
    Возвращает (start_utc, end_utc) для "сегодня" в локальном часовом поясе.
    created_at хранится в UTC, поэтому сдвигаем границы на STAT_TZ_OFFSET.
    """
    now_local = datetime.utcnow() + timedelta(hours=STAT_TZ_OFFSET)
    local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_midnight - timedelta(hours=STAT_TZ_OFFSET)
    return start_utc, start_utc + timedelta(days=1)


def build_today_stats(db) -> str:
    start, end = _today_utc_bounds()

    def in_today(col):
        return (col >= start) & (col < end)

    # Активные пользователи и переводы за сегодня
    active_users = (
        db.query(func.count(func.distinct(UsageEvent.telegram_id)))
        .filter(in_today(UsageEvent.created_at))
        .scalar() or 0
    )
    translations = (
        db.query(func.count(UsageEvent.id))
        .filter(in_today(UsageEvent.created_at))
        .scalar() or 0
    )
    trial_cnt = (
        db.query(func.count(UsageEvent.id))
        .filter(in_today(UsageEvent.created_at), UsageEvent.billing == "trial")
        .scalar() or 0
    )
    paid_cnt = translations - trial_cnt
    conv_cnt = (
        db.query(func.count(UsageEvent.id))
        .filter(in_today(UsageEvent.created_at), UsageEvent.mode == "conversation")
        .scalar() or 0
    )
    seconds_used = (
        db.query(func.coalesce(func.sum(UsageEvent.seconds), 0))
        .filter(in_today(UsageEvent.created_at))
        .scalar() or 0
    )

    # Новые пользователи за сегодня
    new_users = (
        db.query(func.count(User.telegram_id))
        .filter(in_today(User.created_at))
        .scalar() or 0
    )

    # Оплаты и доход за сегодня (по дате оплаты — updated_at у paid)
    paid_today = (
        db.query(Payment)
        .filter(Payment.status == "paid", in_today(Payment.updated_at))
        .all()
    )
    revenue = sum(int(p.amount_usd or 0) for p in paid_today)

    # Всего (для контекста)
    total_users = db.query(func.count(User.telegram_id)).scalar() or 0

    minutes_used = seconds_used // 60
    return (
        "📊 Статистика за сегодня\n"
        f"(день по UTC+{STAT_TZ_OFFSET})\n\n"
        f"👥 Активных юзеров: {active_users}\n"
        f"🆕 Новых юзеров: {new_users}\n"
        f"🎙 Переводов: {translations}\n"
        f"   ├ trial: {trial_cnt}\n"
        f"   ├ платных: {paid_cnt}\n"
        f"   └ в режиме диалога: {conv_cnt}\n"
        f"⏱ Использовано: ~{minutes_used} мин\n"
        f"💳 Оплат: {len(paid_today)} на ${revenue}\n\n"
        f"Всего юзеров в базе: {total_users}"
    )


# =========================================================
# Voice processing
# =========================================================
def process_voice(db, user: "User", chat_id: int, file_id: str, duration: int) -> None:
    b_mode, charge = decide_billing(user, duration)
    if b_mode == "deny":
        tg_send_message(chat_id, "⛔ Not enough balance. Tap “Buy minutes”.", reply_markup=user_keyboard(user))
        return

    audio = tg_download_voice(file_id)
    transcript = openai_transcribe(audio)
    log.info("Voice from %s (%ss): transcript=%r", chat_id, duration, transcript)
    if not transcript:
        raise RuntimeError("Empty voice message")

    if (user.mode or "translate") == "conversation":
        translated, used_caption = _process_conversation_voice(db, user, transcript)
    else:
        translated = openai_translate_text(transcript, lang_name(user.target_lang))
        used_caption = None
    log.info("Voice from %s: translated=%r", chat_id, translated)

    tts = openai_tts(translated)
    apply_billing(db, user, b_mode, charge)
    db.add(UsageEvent(
        telegram_id=chat_id,
        mode=(user.mode or "translate"),
        billing=b_mode,
        seconds=max(MIN_BILLABLE_SECONDS, int(duration)),
    ))
    db.commit()

    if b_mode == "trial":
        cap = f"🎁 Trial left: {user.trial_left}"
    else:
        cap = f"💳 Balance: {user.balance_seconds // 60} min"
    if used_caption:
        cap = f"{used_caption}\n{cap}"
    # Показываем распознанный и переведённый текст — видно, если звук не совпал
    text_block = f"📝 {transcript}\n➡️ {translated}"
    cap = f"{text_block}\n{cap}"
    # Подпись Telegram ограничена ~1024 символами
    if len(cap) > 1000:
        cap = cap[:997] + "..."

    tg_send_voice(chat_id, tts, caption=cap)
    tg_send_message(chat_id, format_status_text(user), reply_markup=user_keyboard(user))


def _process_conversation_voice(db, user: "User", transcript: str) -> Tuple[str, Optional[str]]:
    # В conversation храним ИМЕНА языков (англ.), а не коды — поддержка любого языка
    source_lang = (user.conversation_source_lang or "").strip()
    target_lang = (user.conversation_target_lang or "").strip()
    setup = parse_conversation_setup(transcript)
    log.info(
        "Conversation parse: has_command=%s target=%r msg=%r (pair: %s/%s)",
        setup["has_command"], setup["target_lang"], setup["message_text"], source_lang, target_lang,
    )

    # Команда настройки пары языков
    if setup["has_command"]:
        new_target = setup["target_lang"]
        if not new_target:
            raise RuntimeError(
                "Не понял, на какой язык переводить. Скажите, например: "
                "«переведи на японский, как тебя зовут»."
            )
        msg = setup["message_text"] or transcript
        new_source = detect_language_name(msg)
        if not new_source or _norm_name(new_source) == _norm_name(new_target):
            # подстрахуемся, если детект совпал с target или пуст
            new_source = "English" if _norm_name(new_target) != "english" else "Russian"
        user.conversation_source_lang = new_source
        user.conversation_target_lang = new_target
        db.commit()
        log.info("Conversation pair set: %s -> %s", new_source, new_target)
        translated = openai_translate_text(msg, new_target, source_lang=new_source)
        caption = f"🗣 {new_source} → {new_target}"
        return translated, caption

    # Пара ещё не настроена
    if not source_lang or not target_lang:
        raise RuntimeError(
            "Сначала задайте пару голосом, например: "
            "«переведи на японский, как тебя зовут»."
        )

    incoming = detect_language_name(transcript)
    translate_to, detected_from = resolve_conversation_direction(source_lang, target_lang, incoming)
    log.info("Conversation route: incoming=%s -> translate_to=%s", incoming, translate_to)
    translated = openai_translate_text(
        transcript, translate_to, source_lang=detected_from if detected_from else None
    )
    caption = f"🗣 {detected_from} → {translate_to}"
    return translated, caption


# =========================================================
# FastAPI
# =========================================================
app = FastAPI(title="Lingovox")


@app.on_event("startup")
def startup():
    init_db()


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.get("/", response_class=HTMLResponse)
def landing():
    bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "#"
    langs = ", ".join(name for name, _ in LANGS)
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Lingovox</title><style>
body{{background:#0b1020;color:#e9eefc;font-family:system-ui,sans-serif;padding:40px;margin:0}}
a{{color:#7aa2ff}}.card{{background:#111a33;padding:28px;border-radius:16px;max-width:600px;margin:40px auto;box-shadow:0 10px 40px rgba(0,0,0,.4)}}
.btn{{display:inline-block;margin-top:16px;background:#3b6ef0;color:#fff;padding:12px 22px;border-radius:10px;text-decoration:none}}
</style></head><body><div class="card">
<h1>Lingovox AI</h1><p>AI voice translator for Telegram.</p>
<p><b>Supported:</b> {langs}</p>
<a class="btn" href="{bot_link}">Open Bot</a></div></body></html>"""
    return HTMLResponse(content=html)


@app.get("/terms", response_class=HTMLResponse)
def terms():
    return HTMLResponse("<h1>Terms of Service</h1><p>Lingovox voice translation service.</p>")


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return HTMLResponse("<h1>Privacy Policy</h1><p>Voice data is processed only to provide translations.</p>")


@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    try:
        update = await req.json()
    except Exception:
        return JSONResponse({"ok": True})

    try:
        if "message" in update:
            await _handle_message(update["message"])
        elif "callback_query" in update:
            _handle_callback(update["callback_query"])
    except Exception:
        log.exception("Webhook error")
    return JSONResponse({"ok": True})


async def _handle_message(msg: Dict[str, Any]) -> None:
    chat_id = msg.get("chat", {}).get("id")
    if not chat_id:
        return
    text = (msg.get("text") or "").strip()

    # --- Commands ---
    if text == "/start":
        with SessionLocal() as db:
            u = ensure_user(db, chat_id)
            tg_send_message(chat_id, format_status_text(u), reply_markup=user_keyboard(u))
        return

    if text in ("/help", "/menu"):
        with SessionLocal() as db:
            u = ensure_user(db, chat_id)
            help_text = (
                "ℹ️ Lingovox help\n\n"
                "• Pick a target language, then send voice — I translate & reply with voice.\n"
                "• 🗣 Conversation: say \"Translate to Spanish: hello\" to set a pair, "
                "then speak in either language.\n"
                "• /buy — buy minutes\n"
                "• /support <message> — contact support"
            )
            tg_send_message(chat_id, help_text, reply_markup=user_keyboard(u))
        return

    if text == "/buy":
        tg_send_message(chat_id, "💳 Choose package:", reply_markup=build_packages_keyboard())
        return

    if text.startswith("/support"):
        if text == "/support":
            tg_send_message(chat_id, "🆘 Send: /support <your message>")
        else:
            ticket_text = text.split(" ", 1)[1].strip()
            with SessionLocal() as db:
                t = SupportTicket(telegram_id=chat_id, message=ticket_text)
                db.add(t)
                db.commit()
                db.refresh(t)
                tg_send_message(chat_id, f"✅ Ticket #{t.id} created. We'll get back to you.")
                admin_notify(f"🆘 New ticket #{t.id} from {chat_id}: {ticket_text}")
        return

    if text == "/stat" and is_admin(chat_id):
        with SessionLocal() as db:
            tg_send_message(chat_id, build_today_stats(db))
        return

    if text.startswith("/reply") and is_admin(chat_id):
        parts = text.split(maxsplit=2)
        if len(parts) == 3:
            tid, rmsg = int(parts[1]), parts[2]
            with SessionLocal() as db:
                t = db.get(SupportTicket, tid)
                if t:
                    tg_send_message(t.telegram_id, f"✅ Support reply:\n{rmsg}")
                    t.status = "closed"
                    db.commit()
                    tg_send_message(chat_id, f"✅ Replied to #{tid}")
        return

    # --- Voice ---
    if "voice" in msg:
        v = msg["voice"]
        fid = v["file_id"]
        dur = int(v.get("duration", 0))
        with SessionLocal() as db:
            u = ensure_user(db, chat_id)
            try:
                process_voice(db, u, chat_id, fid, dur)
            except Exception as e:
                log.exception("Voice processing error")
                tg_send_message(chat_id, f"⚠️ {e}", reply_markup=user_keyboard(u))
        return

    # --- Fallback for plain text ---
    if text:
        with SessionLocal() as db:
            u = ensure_user(db, chat_id)
            tg_send_message(
                chat_id,
                "🎙 Send me a *voice* message to translate. Use /help for options.",
                reply_markup=user_keyboard(u),
            )


def _handle_callback(cq: Dict[str, Any]) -> None:
    data = cq.get("data", "")
    chat_id = cq["message"]["chat"]["id"]
    cq_id = cq["id"]

    with SessionLocal() as db:
        u = ensure_user(db, chat_id)

        if data.startswith("lang:"):
            code = data.split(":", 1)[1]
            if code in SUPPORTED_LANG_CODES:
                u.target_lang = code
                u.mode = "translate"
                u.conversation_source_lang = None
                u.conversation_target_lang = None
                db.commit()
                tg_send_message(chat_id, format_status_text(u), reply_markup=user_keyboard(u))

        elif data == "mode:conversation":
            u.mode = "conversation"
            # НЕ сбрасываем уже настроенную пару — удобнее для пользователя
            db.commit()
            tg_send_message(chat_id, format_status_text(u), reply_markup=user_keyboard(u))

        elif data == "conversation:reset":
            u.conversation_source_lang = None
            u.conversation_target_lang = None
            db.commit()
            tg_send_message(chat_id, "🔄 Пара языков сброшена. Скажите команду заново, например: «переведи на японский, как тебя зовут».", reply_markup=user_keyboard(u))

        elif data == "buy:menu":
            tg_send_message(chat_id, "💳 Choose package:", reply_markup=build_packages_keyboard())

        elif data == "buy:back":
            tg_send_message(chat_id, format_status_text(u), reply_markup=user_keyboard(u))

        elif data == "support:menu":
            tg_send_message(chat_id, "🆘 Use /support <message> to contact us.")

        elif data.startswith("paddle:"):
            handle_card_purchase(db, chat_id, data.split(":", 1)[1])

    tg_answer_callback(cq_id)


@app.post(PADDLE_POSTBACK_PATH)
async def paddle_postback(req: Request):
    raw = await req.body()
    sig = req.headers.get("Paddle-Signature", "")
    if not verify_paddle_signature(raw, sig):
        return PlainTextResponse("invalid", status_code=400)
    try:
        data = json.loads(raw.decode())
    except Exception:
        return PlainTextResponse("bad json", status_code=400)
    if data.get("event_type") == "transaction.completed":
        tid = data.get("data", {}).get("id")
        with SessionLocal() as db:
            p = db.query(Payment).filter(Payment.invoice_id == tid).first()
            if p:
                credit_payment_if_needed(db, p)
    return PlainTextResponse("ok")
