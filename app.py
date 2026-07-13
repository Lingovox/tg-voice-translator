import os
import time
import json
import logging
import hashlib
import hmac
import re
from datetime import datetime, timedelta
from collections import OrderedDict
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
# Распознавание речи: основной движок и запасной (включается, если основной выдал кашу)
STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-transcribe").strip()
STT_FALLBACK_MODEL = os.getenv("STT_FALLBACK_MODEL", "whisper-1").strip()

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

# ---- WhatsApp Cloud API ----
WA_TOKEN = os.getenv("WA_TOKEN", "").strip()
WA_PHONE_ID = os.getenv("WA_PHONE_ID", "").strip()
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "lingovox_wa_verify_2024").strip()
WA_API_URL = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"

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
# UI localization (RU / EN, fallback EN)
# =========================================================
UI_DEFAULT = "en"

STRINGS: Dict[str, Dict[str, str]] = {
    # Onboarding / status
    "welcome_title": {
        "en": "🎙 Lingovox — AI voice translator",
        "ru": "🎙 Lingovox — голосовой ИИ-переводчик",
    },
    "welcome_intro": {
        "en": "Send me a voice message and I'll reply with a voice translation.",
        "ru": "Отправьте голосовое — я отвечу голосовым переводом.",
    },
    "status_target": {
        "en": "🌍 Target language: {lang}",
        "ru": "🌍 Язык перевода: {lang}",
    },
    "status_free": {
        "en": "🎁 Free messages left: {n} (≤ {sec}s)",
        "ru": "🎁 Бесплатных сообщений: {n} (≤ {sec} сек)",
    },
    "status_balance": {
        "en": "💳 Balance: {min} min",
        "ru": "💳 Баланс: {min} мин",
    },
    "status_hint": {
        "en": "Send a voice message — I'll translate it and reply with voice.",
        "ru": "Отправьте голосовое — переведу и отвечу голосом.",
    },
    # Conversation
    "conv_title": {
        "en": "🎙 Lingovox — live conversation",
        "ru": "🎙 Lingovox — живой диалог",
    },
    "conv_active": {
        "en": "🗣 Conversation: {a} ↔ {b}\nSpeak from either side — I detect the language and translate.",
        "ru": "🗣 Диалог: {a} ↔ {b}\nГоворите с любой стороны — определю язык и переведу.",
    },
    "conv_setup": {
        "en": "🗣 Conversation mode is on.\nSay a command, e.g.:\n«translate to Japanese, what is your name»\nAny language works. I'll remember the pair and translate both ways.",
        "ru": "🗣 Режим диалога включён.\nСкажите команду голосом, например:\n«переведи на японский, как тебя зовут»\nЛюбой язык мира. Запомню пару и буду переводить в обе стороны.",
    },
    "conv_reset_done": {
        "en": "🔄 Pair reset. Say the command again, e.g. «translate to Japanese, what is your name».",
        "ru": "🔄 Пара сброшена. Скажите команду заново, например «переведи на японский, как тебя зовут».",
    },
    "conv_need_target": {
        "en": "Couldn't tell which language to translate to. Try: «translate to Japanese, what is your name».",
        "ru": "Не понял, на какой язык переводить. Скажите, например: «переведи на японский, как тебя зовут».",
    },
    "conv_not_set": {
        "en": "Set a pair first by voice, e.g.: «translate to Japanese, what is your name».",
        "ru": "Сначала задайте пару голосом, например: «переведи на японский, как тебя зовут».",
    },
    # Buttons
    "btn_conversation": {"en": "🗣 Conversation", "ru": "🗣 Диалог"},
    "btn_target_lang": {"en": "🌍 Target language", "ru": "🌍 Язык перевода"},
    "btn_reset_conv": {"en": "🔄 Reset conversation", "ru": "🔄 Сбросить диалог"},
    "btn_buy": {"en": "💳 Buy minutes", "ru": "💳 Купить минуты"},
    "btn_support": {"en": "🆘 Support", "ru": "🆘 Поддержка"},
    "btn_help": {"en": "ℹ️ Help", "ru": "ℹ️ Помощь"},
    "btn_back": {"en": "⬅️ Back", "ru": "⬅️ Назад"},
    # Menus / messages
    "choose_lang": {"en": "🌍 Choose target language:", "ru": "🌍 Выберите язык перевода:"},
    "choose_package": {"en": "💳 Choose a package:", "ru": "💳 Выберите пакет:"},
    "pkg_card": {
        "en": "💳 Card — {min} min — ${usd}",
        "ru": "💳 Карта — {min} мин — ${usd}",
    },
    "help_text": {
        "en": (
            "ℹ️ How to use Lingovox\n\n"
            "• Pick a target language, then send a voice message — I translate and reply with voice.\n"
            "• 🗣 Conversation: say «translate to Spanish: hello» to set a pair, then speak in either language.\n"
            "• 💳 Buy minutes — top up your balance.\n"
            "• 🆘 /support <message> — contact us."
        ),
        "ru": (
            "ℹ️ Как пользоваться Lingovox\n\n"
            "• Выберите язык перевода и отправьте голосовое — переведу и отвечу голосом.\n"
            "• 🗣 Диалог: скажите «переведи на испанский: привет», чтобы задать пару, затем говорите на любом из двух языков.\n"
            "• 💳 Купить минуты — пополнить баланс.\n"
            "• 🆘 /support <сообщение> — связаться с нами."
        ),
    },
    "support_prompt": {
        "en": "🆘 Send: /support <your message>",
        "ru": "🆘 Напишите: /support <ваше сообщение>",
    },
    "support_created": {
        "en": "✅ Ticket #{id} created. We'll get back to you.",
        "ru": "✅ Обращение #{id} создано. Мы ответим вам.",
    },
    "no_balance": {
        "en": "⛔ Not enough balance. Tap “Buy minutes”.",
        "ru": "⛔ Недостаточно баланса. Нажмите «Купить минуты».",
    },
    "voice_only": {
        "en": "🎙 Send me a voice message to translate. Tap ℹ️ Help for options.",
        "ru": "🎙 Отправьте голосовое для перевода. Нажмите ℹ️ Помощь для подсказок.",
    },
    "voice_unclear": {
        "en": "🤔 Couldn't make out the audio clearly — it may be noisy or in a hard-to-recognize language. Try recording again, a bit slower and clearer. (You weren't charged.)",
        "ru": "🤔 Не удалось разобрать аудио — возможно, шумно или язык трудно распознаётся. Попробуйте записать ещё раз, чуть медленнее и чётче. (Списание не произошло.)",
    },
    "pay_card_link": {"en": "💳 Pay with card:\n{url}", "ru": "💳 Оплата картой:\n{url}"},
    "pay_received": {
        "en": "✅ Payment received!\nCredited: {min} min",
        "ru": "✅ Оплата получена!\nЗачислено: {min} мин",
    },
    "err_generic": {"en": "⚠️ {msg}", "ru": "⚠️ {msg}"},
    "err_pay_config": {
        "en": "⚠️ Card payments are not configured: {what}",
        "ru": "⚠️ Оплата картой не настроена: {what}",
    },
    "err_pay_create": {
        "en": "⚠️ Could not create card checkout. Try again later.",
        "ru": "⚠️ Не удалось создать оплату. Попробуйте позже.",
    },
}


def pick_ui_lang(language_code: str) -> str:
    """Telegram language_code -> 'ru' или 'en' (fallback en)."""
    base = (language_code or "").strip().lower().split("-", 1)[0]
    return "ru" if base == "ru" else "en"


def t(key: str, ui_lang: str = UI_DEFAULT, **kwargs) -> str:
    entry = STRINGS.get(key, {})
    template = entry.get(ui_lang) or entry.get(UI_DEFAULT) or key
    try:
        return template.format(**kwargs) if kwargs else template
    except Exception:
        return template


def ui_of(user: "User") -> str:
    return (getattr(user, "ui_lang", None) or UI_DEFAULT)


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
    # Язык интерфейса бота: "ru" или "en"
    ui_lang = Column(String, nullable=False, default="en")
    # В режиме conversation храним коды языков из списка LANGS
    conversation_source_lang = Column(String, nullable=True)
    conversation_target_lang = Column(String, nullable=True)
    # WhatsApp поля (nullable — Telegram-юзеры их не используют)
    platform = Column(String, nullable=True, default="tg")   # "tg" | "wa"
    wa_phone = Column(String, nullable=True, index=True)     # номер 998901234567
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
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS ui_lang VARCHAR DEFAULT 'en'")
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS conversation_source_lang VARCHAR")
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS conversation_target_lang VARCHAR")
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_messages INTEGER DEFAULT 0")
        conn.exec_driver_sql("ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider VARCHAR DEFAULT 'paddle'")
        conn.exec_driver_sql("ALTER TABLE payments ADD COLUMN IF NOT EXISTS external_id VARCHAR")
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS platform VARCHAR DEFAULT 'tg'")
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS wa_phone VARCHAR")


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


def tg_edit_message(chat_id: int, message_id: int, text: str,
                    reply_markup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    res = tg_request("editMessageText", payload)
    # Если редактирование не удалось (например, текст не изменился) — не критично
    if not res.get("ok"):
        log.info("editMessageText not applied: %s", res.get("description"))
    return res


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
def build_main_keyboard(user: "User") -> Dict[str, Any]:
    ui = ui_of(user)
    mode = user.mode or "translate"
    conversation_ready = bool(user.conversation_source_lang and user.conversation_target_lang)
    rows: List[List[Dict[str, str]]] = []

    conv_prefix = "✅ " if mode == "conversation" else ""
    rows.append([{"text": f"{conv_prefix}{t('btn_conversation', ui)}", "callback_data": "mode:conversation"}])

    # Кнопка выбора языка перевода видна всегда (в т.ч. в режиме диалога)
    rows.append([{"text": t("btn_target_lang", ui), "callback_data": "lang:menu"}])
    # В режиме диалога с настроенной парой даём сброс
    if mode == "conversation" and conversation_ready:
        rows.append([{"text": t("btn_reset_conv", ui), "callback_data": "conversation:reset"}])

    rows.append([
        {"text": t("btn_buy", ui), "callback_data": "buy:menu"},
        {"text": t("btn_help", ui), "callback_data": "help:menu"},
    ])
    rows.append([{"text": t("btn_support", ui), "callback_data": "support:menu"}])
    return {"inline_keyboard": rows}


def build_lang_keyboard(user: "User") -> Dict[str, Any]:
    ui = ui_of(user)
    rows: List[List[Dict[str, str]]] = []
    for i in range(0, len(LANGS), 2):
        row = []
        for title, code in LANGS[i:i + 2]:
            prefix = "✅ " if code == user.target_lang else ""
            row.append({"text": f"{prefix}{title}", "callback_data": f"lang:set:{code}"})
        rows.append(row)
    rows.append([{"text": t("btn_back", ui), "callback_data": "menu:main"}])
    return {"inline_keyboard": rows}


def build_packages_keyboard(user: "User") -> Dict[str, Any]:
    ui = ui_of(user)
    rows = []
    for code in ("P60", "P180", "P600"):
        pkg = PACKAGES[code]
        label = t("pkg_card", ui, min=pkg["minutes"], usd=pkg["usd"])
        rows.append([{"text": label, "callback_data": f"paddle:{code}"}])
    rows.append([{"text": t("btn_back", ui), "callback_data": "menu:main"}])
    return {"inline_keyboard": rows}


def user_keyboard(user: "User") -> Dict[str, Any]:
    return build_main_keyboard(user)


def format_status_text(user: "User") -> str:
    ui = ui_of(user)
    bal_min = max(0, int(user.balance_seconds or 0)) // 60
    free_line = t("status_free", ui, n=user.trial_left, sec=TRIAL_MAX_SECONDS)
    bal_line = t("status_balance", ui, min=bal_min)

    if (user.mode or "translate") == "conversation":
        if user.conversation_source_lang and user.conversation_target_lang:
            conv_line = t("conv_active", ui, a=user.conversation_source_lang, b=user.conversation_target_lang)
        else:
            conv_line = t("conv_setup", ui)
        return f"{t('conv_title', ui)}\n\n{conv_line}\n\n{free_line}\n{bal_line}"

    return (
        f"{t('welcome_title', ui)}\n\n"
        f"{t('status_target', ui, lang=lang_name(user.target_lang))}\n"
        f"{free_line}\n{bal_line}\n\n"
        f"{t('status_hint', ui)}"
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


def openai_transcribe(ogg_bytes: bytes, model: str = "whisper-1") -> str:
    url = "https://api.openai.com/v1/audio/transcriptions"
    files = {
        "file": ("audio.ogg", ogg_bytes, "audio/ogg"),
        "model": (None, model),
    }
    if "gpt-4o" in (model or "").lower():
        # У gpt-4o-transcribe бывает обрезка длинного аудио. Формат text + авто-чанкинг
        # заставляют модель обработать запись целиком, а не первые секунды.
        files["response_format"] = (None, "text")
        files["chunking_strategy"] = (None, "auto")
    else:
        files["response_format"] = (None, "json")

    r = requests.post(url, headers=_openai_headers(), files=files, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Transcription failed ({model}): {r.text[:300]}")
    # text-формат возвращает чистый текст, json — поле text
    ctype = (r.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        return str(r.json().get("text") or "").strip()
    return (r.text or "").strip()


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


def transcript_looks_valid(text: str) -> bool:
    """
    Лёгкая проверка: осмысленный ли распознанный текст, или это каша
    (Whisper спутал язык / шум). Дешёвый вызов gpt-4o-mini.
    Возвращает True если текст связный, False если мусор.
    При ошибке проверки возвращает True (не блокируем по сбою сети).
    """
    t = (text or "").strip()
    # Совсем короткое — не гоняем проверку, пропускаем
    if len(t) < 8:
        return True
    url = "https://api.openai.com/v1/chat/completions"
    prompt = (
        "You check speech-to-text output. The text may be in ANY language "
        "(including less common ones like Uzbek, Kazakh, Georgian) and may be short. "
        "Most text is fine — your job is only to catch CLEARLY BROKEN transcription.\n"
        "Return {\"ok\": false} ONLY if the text is obvious gibberish: a chaotic mix of "
        "fragments from different languages that together form no meaning, or strings of "
        "non-existent word-forms. When unsure, return {\"ok\": true}.\n"
        "A normal, meaningful sentence in any single language is ALWAYS {\"ok\": true}, "
        "even if short or in a rare language.\n"
        "Return ONLY JSON like {\"ok\": true} or {\"ok\": false}.\n\n"
        f"Text:\n{t}"
    )
    body = {
        "model": OPENAI_TEXT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 20,
    }
    try:
        r = _openai_post(url, json_body=body, timeout=30)
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        return bool(out.get("ok", True))
    except Exception as e:
        log.warning("transcript_looks_valid failed (allowing): %s", e)
        return True


def _is_accurate_model(model: str) -> bool:
    """Точные модели, чьему результату доверяем без придирчивой проверки."""
    return "gpt-4o" in (model or "").lower()


def transcribe_with_fallback(audio: bytes, chat_id: int) -> Tuple[str, bool]:
    """
    Распознаёт аудио основным движком. Если основной — точная модель (gpt-4o),
    доверяем её результату сразу. Если основной — Whisper и он выдал кашу,
    автоматически повторяем на запасном движке.
    Возвращает (text, ok), где ok=False означает, что осмысленный текст получить не удалось.
    """
    # 1) Основной движок
    text = openai_transcribe(audio, model=STT_MODEL)
    log.info("Voice from %s: [%s] transcript=%r", chat_id, STT_MODEL, text)
    if text:
        # Точной модели доверяем без проверки; иначе проверяем на кашу
        if _is_accurate_model(STT_MODEL) or transcript_looks_valid(text):
            return text, True

    # 2) Основной пуст или выдал кашу → пробуем запасной (если он другой)
    if STT_FALLBACK_MODEL and STT_FALLBACK_MODEL != STT_MODEL:
        try:
            text2 = openai_transcribe(audio, model=STT_FALLBACK_MODEL)
            log.info("Voice from %s: [%s fallback] transcript=%r", chat_id, STT_FALLBACK_MODEL, text2)
            if text2:
                if _is_accurate_model(STT_FALLBACK_MODEL) or transcript_looks_valid(text2):
                    return text2, True
                return text2, False
        except Exception as e:
            log.warning("Fallback STT failed: %s", e)

    return text, False


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
        t("pay_received", ui_of(user), min=pkg["minutes"]),
        reply_markup=user_keyboard(user),
    )
    return True


# =========================================================
# Payment callback handlers (создание инвойсов)
# =========================================================
def handle_card_purchase(db, chat_id: int, package_code: str) -> None:
    u = ensure_user(db, chat_id)
    ui = ui_of(u)
    pkg = PACKAGES.get(package_code)
    if not pkg:
        tg_send_message(chat_id, t("err_pay_create", ui))
        return
    missing = paddle_env_missing()
    if missing:
        tg_send_message(chat_id, t("err_pay_config", ui, what=", ".join(missing)))
        return
    try:
        res = paddle_create_transaction(package_code, chat_id)
    except Exception as e:
        log.warning("Paddle transaction error: %s", e)
        tg_send_message(chat_id, t("err_pay_create", ui))
        return
    if not res.get("ok"):
        log.warning("Paddle transaction failed: %s", res)
        tg_send_message(chat_id, t("err_pay_create", ui))
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
        tg_send_message(chat_id, t("pay_card_link", ui, url=checkout))
    else:
        tg_send_message(chat_id, t("err_pay_create", ui))


# =========================================================
# Logic helpers
# =========================================================
def ensure_user(db, chat_id: int, language_code: Optional[str] = None) -> "User":
    u = db.get(User, int(chat_id))
    if not u:
        ui = pick_ui_lang(language_code or "")
        u = User(
            telegram_id=int(chat_id), target_lang="en", trial_left=TRIAL_LIMIT,
            mode="translate", ui_lang=ui,
        )
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
        tg_send_message(chat_id, t("no_balance", ui_of(user)), reply_markup=user_keyboard(user))
        return

    audio = tg_download_voice(file_id)
    transcript, ok = transcribe_with_fallback(audio, chat_id)
    if not transcript:
        raise RuntimeError("Empty voice message")

    # Если ни основной, ни запасной движок не дали осмысленного текста —
    # не переводим мусор, честно просим повторить. Биллинг НЕ списываем.
    if not ok:
        log.info("Voice from %s: transcript rejected after fallback", chat_id)
        tg_send_message(chat_id, t("voice_unclear", ui_of(user)), reply_markup=user_keyboard(user))
        return

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
            raise RuntimeError(t("conv_need_target", ui_of(user)))
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
        raise RuntimeError(t("conv_not_set", ui_of(user)))

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


SUPPORT_EMAIL = "neural.flow.io@tutamail.com"
SITE_NAME = "Lingovox"


def _page(title: str, body: str) -> str:
    bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "#"
    year = datetime.utcnow().year
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title} — {SITE_NAME}</title>
<style>
:root{{--bg:#0b1020;--card:#121a33;--ink:#e9eefc;--muted:#9fb0d8;--accent:#3b6ef0;--accent2:#7aa2ff;--line:#23304f}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;line-height:1.6}}
a{{color:var(--accent2);text-decoration:none}}
a:hover{{text-decoration:underline}}
.wrap{{max-width:860px;margin:0 auto;padding:32px 20px 64px}}
.nav{{display:flex;align-items:center;justify-content:space-between;padding:18px 0}}
.brand{{font-weight:700;font-size:20px;letter-spacing:.2px}}
.brand span{{color:var(--accent2)}}
.hero{{text-align:center;padding:48px 16px 28px}}
.hero h1{{font-size:40px;line-height:1.15;margin:0 0 14px}}
.hero p{{font-size:18px;color:var(--muted);max-width:560px;margin:0 auto 26px}}
.btn{{display:inline-block;background:var(--accent);color:#fff;padding:14px 30px;border-radius:12px;font-weight:600;font-size:16px}}
.btn:hover{{background:#2f5fd6;text-decoration:none}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin:36px 0}}
.feat{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px}}
.feat h3{{margin:0 0 8px;font-size:17px}}
.feat p{{margin:0;color:var(--muted);font-size:15px}}
.section-title{{text-align:center;font-size:26px;margin:48px 0 8px}}
.section-sub{{text-align:center;color:var(--muted);margin:0 0 28px}}
.prices{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px}}
.price{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px;text-align:center}}
.price .min{{font-size:15px;color:var(--muted)}}
.price .amt{{font-size:32px;font-weight:700;margin:6px 0}}
.price .per{{font-size:13px;color:var(--muted)}}
.langs{{text-align:center;color:var(--muted);margin:26px auto;max-width:640px}}
.doc h1{{font-size:30px;margin:8px 0 4px}}
.doc .upd{{color:var(--muted);font-size:14px;margin-bottom:24px}}
.doc h2{{font-size:19px;margin:28px 0 8px}}
.doc p,.doc li{{color:#cdd8f4}}
.doc ul{{padding-left:20px}}
.footer{{border-top:1px solid var(--line);margin-top:48px;padding-top:22px;text-align:center;color:var(--muted);font-size:14px}}
.footer a{{margin:0 10px}}
</style></head><body><div class="wrap">
<div class="nav">
  <div class="brand">🎙 Lingo<span>vox</span></div>
  <a class="brand-link" href="{bot_link}">Open bot →</a>
</div>
{body}
<div class="footer">
  © {year} {SITE_NAME} ·
  <a href="/">Home</a><a href="/blog">Blog</a><a href="/terms">Terms</a><a href="/privacy">Privacy</a>
  <br/>Contact: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>
</div>
</div></body></html>"""


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.get("/", response_class=HTMLResponse)
def landing():
    bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "#"
    langs = ", ".join(name for name, _ in LANGS)

    price_cards = ""
    for code in ("P60", "P180", "P600"):
        pkg = PACKAGES[code]
        per = pkg["usd"] / pkg["minutes"]
        price_cards += (
            f'<div class="price"><div class="min">{pkg["minutes"]} minutes</div>'
            f'<div class="amt">${pkg["usd"]}</div>'
            f'<div class="per">${per:.2f} / min</div></div>'
        )

    body = f"""
<div class="hero">
  <h1>Speak any language.<br/>Be understood instantly.</h1>
  <p>Lingovox is an AI voice translator in Telegram. Send a voice message — get a natural voice translation back in seconds.</p>
  <a class="btn" href="{bot_link}">Start translating →</a>
</div>

<div class="grid">
  <div class="feat"><h3>🎙 Voice in, voice out</h3><p>Speak naturally. Lingovox transcribes, translates and replies with a clear voice message.</p></div>
  <div class="feat"><h3>🗣 Live conversation</h3><p>Set a language pair by voice once, then talk back and forth — it auto-detects each side.</p></div>
  <div class="feat"><h3>🌍 Any language</h3><p>Translate into dozens of languages, with optional voice replies in your target language.</p></div>
  <div class="feat"><h3>🎁 Free to try</h3><p>Your first messages are on us. Top up minutes only when you need more.</p></div>
</div>

<h2 class="section-title">Simple pricing</h2>
<p class="section-sub">Pay only for what you use. No subscription.</p>
<div class="prices">{price_cards}</div>

<div class="langs"><b>Popular target languages:</b><br/>{langs} — and many more in conversation mode.</div>

<div class="hero" style="padding-top:8px">
  <a class="btn" href="{bot_link}">Open Lingovox in Telegram</a>
</div>
"""
    return HTMLResponse(content=_page("AI voice translator", body))


@app.get("/terms", response_class=HTMLResponse)
def terms():
    body = f"""
<div class="doc">
<h1>Terms of Service</h1>
<div class="upd">Last updated: {datetime.utcnow():%B %d, %Y}</div>

<p>These Terms of Service ("Terms") govern your use of {SITE_NAME} (the "Service"), an AI-powered voice translation bot available through Telegram. By using the Service, you agree to these Terms.</p>

<h2>1. The Service</h2>
<p>{SITE_NAME} transcribes voice messages, translates the text, and returns a synthesized voice translation. The Service relies on third-party AI providers to process audio and text. Translation accuracy is provided on a best-effort basis and may contain errors; do not rely on it for critical, legal, medical, or safety-related communication.</p>

<h2>2. Free trial and paid minutes</h2>
<p>New users receive a limited number of free messages. Beyond the trial, the Service is used by purchasing minute packages. Minutes are consumed based on the length of each processed voice message. Prices are shown in the bot before purchase and are charged in US dollars.</p>

<h2>3. Payments and refunds</h2>
<p>Payments are processed by our authorized reseller and Merchant of Record, Paddle.com, which handles billing, payment, and related support. Purchased minutes are added to your balance immediately after successful payment. Because access is granted instantly, purchases are generally non-refundable once minutes have been used. If you experience a billing problem or believe a charge was made in error, contact us at <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a> and we will review your request in good faith.</p>

<h2>4. Acceptable use</h2>
<p>You agree not to use the Service to process unlawful content, to infringe the rights of others, or to attempt to disrupt, reverse-engineer, or overload the Service. We may suspend access for abuse or fraudulent activity.</p>

<h2>5. Availability</h2>
<p>We aim to keep the Service available but do not guarantee uninterrupted operation. The Service may be modified, suspended, or discontinued at any time. Features and pricing may change.</p>

<h2>6. Limitation of liability</h2>
<p>The Service is provided "as is" without warranties of any kind. To the maximum extent permitted by law, {SITE_NAME} shall not be liable for any indirect, incidental, or consequential damages arising from the use of, or inability to use, the Service.</p>

<h2>7. Contact</h2>
<p>Questions about these Terms can be sent to <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>, or via the Support button inside the bot.</p>
</div>
"""
    return HTMLResponse(content=_page("Terms of Service", body))


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    body = f"""
<div class="doc">
<h1>Privacy Policy</h1>
<div class="upd">Last updated: {datetime.utcnow():%B %d, %Y}</div>

<p>This Privacy Policy explains what information {SITE_NAME} (the "Service") collects and how it is used. By using the Service, you agree to this policy.</p>

<h2>1. Information we collect</h2>
<ul>
  <li><b>Telegram identifier:</b> your numeric Telegram ID, used to maintain your balance, settings, and language preference.</li>
  <li><b>Settings:</b> your selected interface and target languages, trial and balance state.</li>
  <li><b>Voice messages:</b> audio you send is processed to produce a translation. Support requests you submit are stored so we can respond.</li>
  <li><b>Payment records:</b> transaction status and package details. Card details are handled entirely by our payment processor and are never stored by us.</li>
</ul>

<h2>2. How we use information</h2>
<p>We use this information solely to operate the Service: to transcribe and translate your messages, track your balance, process payments, provide support, and prevent abuse. We do not sell your data or use it for advertising.</p>

<h2>3. Third-party processors</h2>
<p>To provide the Service we share the minimum necessary data with: AI providers (OpenAI) to transcribe, translate, and synthesize speech; Telegram, through which the Service is delivered; and Paddle.com, our Merchant of Record, to process payments. Each processes data under its own terms and privacy policy.</p>

<h2>4. Voice data retention</h2>
<p>Audio is processed to generate your translation and is not retained longer than necessary to provide the result. We do not build voice profiles or use your audio to train models.</p>

<h2>5. Your rights</h2>
<p>You may request deletion of your account data at any time by contacting us. Some records (such as payment history) may be retained where required for legal or accounting purposes.</p>

<h2>6. Contact</h2>
<p>For privacy questions or data deletion requests, email <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>.</p>
</div>
"""
    return HTMLResponse(content=_page("Privacy Policy", body))


# =========================================================
# Blog
# =========================================================
# Каждая статья: slug, title (для <h1> и SEO), date, excerpt (для списка),
# body_html (тело в простом HTML — <p>, <h2>, <ul><li>).
BLOG_POSTS: List[Dict[str, str]] = [
    {
        "slug": "supplier-turkey-google-translate",
        "title": "Как мы переписывались с поставщиком из Турции, когда ни он, ни я не знаем английского",
        "date": "2026-06-08",
        "excerpt": "Честная история о том, как выглядит переписка с зарубежным поставщиком, когда иностранный язык хромает у обеих сторон — и почему мы в итоге перешли на голосовые.",
        "body_html": """
<p>Небольшая честная история про то, как устроено общение с зарубежным поставщиком, когда у обеих сторон с иностранными языками так себе. Без success story с ростом оборота в три раза — просто как было на самом деле.</p>

<h2>Контекст</h2>
<p>Я занимаюсь сервисом банкоматов — это в том числе запчасти. Часть из них логично искать не у местных перекупов, а напрямую у зарубежных поставщиков, в нашем случае — в Турции. Турция тут удобна: возят быстрее и дешевле, чем из Китая, и по технике для банкоматов там есть с кем работать. Проблема всплыла ровно в тот момент, когда дошло до переписки.</p>

<h2>Английский «через раз» с обеих сторон</h2>
<p>Сразу честно: я по-английски не оратор. Но и турецкий коллега — тоже. И вот это, как ни странно, и есть самая типичная ситуация в реальной закупке. Не «русский предприниматель пишет носителю английского», а двое людей, для которых английский неродной, пытаются понять друг друга.</p>
<p>Довольно быстро стало видно, что он отвечает мне через Google Translate. Я, разумеется, делал то же самое. То есть с обеих сторон в переписке сидел один и тот же автопереводчик, и мы оба делали вид, что «пишем по-английски».</p>
<p>Работало это так: приходит его сообщение — копирую, вставляю в переводчик, читаю по-русски. Пишу ответ по-русски, перевожу обратно, отправляю. А он на той стороне повторяет всё то же самое. Каждое сообщение — прогон через переводчик туда-обратно с двух сторон. По смыслу терпимо, технические слова он понимал. Но это долго: любое «да, эта деталь, но уточни артикул» проходило четыре прогона. На один вопрос — пять действий.</p>

<h2>Перешли на голосовые — стало быстрее</h2>
<p>В какой-то момент мы перешли на голосовые сообщения. И разница почувствовалась сразу — не в «качестве перевода», а в скорости и естественности. Логика простая: надиктовал голосом то, что хотел сказать, получил перевод на турецком, отправил. Не нужно печатать, копировать, вставлять, гонять текст руками.</p>
<p>Этот переход и подтолкнул меня сделать инструмент под себя — телеграм-бот, который берёт голосовое, распознаёт речь, переводит и отдаёт обратно голосом на нужном языке. Голосовое от поставщика приходит на понятном мне языке, моё уходит уже переведённым. Не революция в ВЭД — просто убрало рутину: вместо «скопируй-вставь-прочитай-напиши-переведи» стало «послушал — надиктовал».</p>

<h2>Чем закончилось</h2>
<p>Честно — пока ничем громким. У нас на текущий момент нет острой потребности именно в том, что предлагают турецкие коллеги, так что активная переписка приостановилась сама собой. Но сам подход к общению с иноязычным поставщиком я для себя закрыл: когда понадобится снова, копипаст-марафон через переводчик мне больше не грозит.</p>

<h2>Что я вынес из этой истории</h2>
<ul>
<li>Скорее всего, ваш поставщик тоже переводит машиной — не нужно стесняться «кривого английского», с той стороны ровно то же самое.</li>
<li>Текстовый перевод съедает время не на переводе, а на рутине: само качество перевода обычно приемлемое, бесит ручной цикл копировать-вставить с двух сторон.</li>
<li>Голос быстрее текста, когда обе стороны всё равно переводят машиной. Надиктовать проще, чем напечатать и прогнать через переводчик.</li>
</ul>
<p>Если кому-то интересен тот самый бот, который я в итоге сделал под эту задачу, — он называется Lingovox, работает в Telegram: берёт голосовое и отвечает голосовым переводом, в том числе в режиме диалога, когда обе стороны говорят на разных языках.</p>
""",
    },
]

BLOG_INDEX = {p["slug"]: p for p in BLOG_POSTS}


@app.get("/blog", response_class=HTMLResponse)
def blog_index():
    bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "#"
    items = ""
    for p in BLOG_POSTS:
        items += (
            f'<a class="post-card" href="/blog/{p["slug"]}">'
            f'<div class="post-date">{p["date"]}</div>'
            f'<h3>{p["title"]}</h3>'
            f'<p>{p["excerpt"]}</p></a>'
        )
    if not items:
        items = '<p style="color:var(--muted)">Articles are coming soon.</p>'
    body = f"""
<div class="hero" style="padding:32px 16px 8px">
  <h1 style="font-size:32px">Blog</h1>
  <p>Notes on building Lingovox and translating across languages.</p>
</div>
<div class="postlist">{items}</div>
<style>
.postlist{{display:flex;flex-direction:column;gap:16px;margin:24px 0}}
.post-card{{display:block;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;color:inherit}}
.post-card:hover{{border-color:var(--accent);text-decoration:none}}
.post-card h3{{margin:6px 0 8px;font-size:19px;color:var(--ink)}}
.post-card p{{margin:0;color:var(--muted);font-size:15px}}
.post-date{{color:var(--muted);font-size:13px}}
</style>
"""
    return HTMLResponse(content=_page("Blog", body))


@app.get("/blog/{slug}", response_class=HTMLResponse)
def blog_post(slug: str):
    post = BLOG_INDEX.get(slug)
    if not post:
        body = ('<div class="doc"><h1>Not found</h1>'
                '<p>This article doesn\'t exist. <a href="/blog">Back to blog</a>.</p></div>')
        return HTMLResponse(content=_page("Not found", body), status_code=404)
    bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "#"
    body = f"""
<div class="doc">
<div style="margin-bottom:14px"><a href="/blog">← Blog</a></div>
<h1>{post["title"]}</h1>
<div class="upd">{post["date"]}</div>
{post["body_html"]}
<div class="hero" style="padding:28px 16px 8px">
  <a class="btn" href="{bot_link}">Try Lingovox in Telegram →</a>
</div>
</div>
"""
    return HTMLResponse(content=_page(post["title"], body))


_processed_updates: "OrderedDict[int, float]" = OrderedDict()
_PROCESSED_MAX = 2000


def _already_processed(update_id: Optional[int]) -> bool:
    """Защита от повторной доставки одного и того же update от Telegram."""
    if update_id is None:
        return False
    if update_id in _processed_updates:
        return True
    _processed_updates[update_id] = time.time()
    # Не даём множеству расти бесконечно
    while len(_processed_updates) > _PROCESSED_MAX:
        _processed_updates.popitem(last=False)
    return False


@app.get("/pay", response_class=HTMLResponse)
def pay_page(request: Request):
    """Страница оплаты: открывает Paddle-чекаут по _ptxn из URL.
    Paddle сам добавляет ?_ptxn=transaction_id к checkout.url."""
    ptxn = (request.query_params.get("_ptxn") or "").strip()
    paddle_env_js = "sandbox" if PADDLE_ENV != "live" else "production"
    bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "#"

    if not ptxn or not PADDLE_CLIENT_TOKEN:
        body = """
<div class="hero">
  <h1>Payment link error</h1>
  <p>This payment link is missing or invalid. Please go back to the bot and tap a package again.</p>
  <a class="btn" href="%s">Back to Lingovox</a>
</div>""" % bot_link
        return HTMLResponse(content=_page("Payment", body), status_code=400)

    # Страница с Paddle.js: инициализируем и сразу открываем чекаут по transactionId.
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Lingovox — Checkout</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0f1f17;
         color:#e8f0ea; display:flex; min-height:100vh; align-items:center;
         justify-content:center; margin:0; text-align:center; padding:24px; }}
  .box {{ max-width:420px; }}
  h1 {{ font-size:20px; margin-bottom:8px; }}
  p {{ opacity:.8; line-height:1.5; }}
  a {{ color:#7fd1a8; }}
  .spin {{ margin:20px auto; width:34px; height:34px; border:3px solid #2c4a3a;
           border-top-color:#7fd1a8; border-radius:50%; animation:r 1s linear infinite; }}
  @keyframes r {{ to {{ transform:rotate(360deg); }} }}
</style>
<script src="https://cdn.paddle.com/paddle/v2/paddle.js"></script>
</head>
<body>
<div class="box">
  <div class="spin"></div>
  <h1>Opening secure checkout…</h1>
  <p id="msg">If the payment window doesn't appear, <a href="#" onclick="openCheckout();return false;">tap here</a>.
  Having trouble? <a href="{bot_link}">Return to the bot</a>.</p>
</div>
<script>
  function openCheckout() {{
    try {{
      Paddle.Checkout.open({{ transactionId: "{ptxn}" }});
    }} catch (e) {{
      document.getElementById('msg').textContent = 'Could not open checkout. Please return to the bot and try again.';
    }}
  }}
  Paddle.Environment.set("{paddle_env_js}");
  Paddle.Initialize({{ token: "{PADDLE_CLIENT_TOKEN}" }});
  window.addEventListener('load', openCheckout);
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    try:
        update = await req.json()
    except Exception:
        return JSONResponse({"ok": True})

    # Telegram повторяет доставку, если не дождался ответа — игнорируем дубли
    if _already_processed(update.get("update_id")):
        log.info("Duplicate update %s ignored", update.get("update_id"))
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
    lang_code = (msg.get("from") or {}).get("language_code", "")

    # --- Commands ---
    if text == "/start":
        with SessionLocal() as db:
            u = ensure_user(db, chat_id, lang_code)
            tg_send_message(chat_id, format_status_text(u), reply_markup=user_keyboard(u))
        return

    if text in ("/help", "/menu"):
        with SessionLocal() as db:
            u = ensure_user(db, chat_id, lang_code)
            tg_send_message(chat_id, t("help_text", ui_of(u)), reply_markup=user_keyboard(u))
        return

    if text == "/buy":
        with SessionLocal() as db:
            u = ensure_user(db, chat_id, lang_code)
            tg_send_message(chat_id, t("choose_package", ui_of(u)), reply_markup=build_packages_keyboard(u))
        return

    if text.startswith("/support"):
        with SessionLocal() as db:
            u = ensure_user(db, chat_id, lang_code)
            ui = ui_of(u)
            if text == "/support":
                tg_send_message(chat_id, t("support_prompt", ui))
            else:
                ticket_text = text.split(" ", 1)[1].strip()
                ticket = SupportTicket(telegram_id=chat_id, message=ticket_text)
                db.add(ticket)
                db.commit()
                db.refresh(ticket)
                tg_send_message(chat_id, t("support_created", ui, id=ticket.id))
                admin_notify(f"🆘 New ticket #{ticket.id} from {chat_id}: {ticket_text}")
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
                ticket = db.get(SupportTicket, tid)
                if ticket:
                    tg_send_message(ticket.telegram_id, f"✅ Support reply:\n{rmsg}")
                    ticket.status = "closed"
                    db.commit()
                    tg_send_message(chat_id, f"✅ Replied to #{tid}")
        return

    # --- Voice ---
    if "voice" in msg:
        v = msg["voice"]
        fid = v["file_id"]
        dur = int(v.get("duration", 0))
        with SessionLocal() as db:
            u = ensure_user(db, chat_id, lang_code)
            try:
                process_voice(db, u, chat_id, fid, dur)
            except Exception as e:
                log.exception("Voice processing error")
                tg_send_message(chat_id, t("err_generic", ui_of(u), msg=str(e)), reply_markup=user_keyboard(u))
        return

    # --- Fallback for plain text ---
    if text:
        with SessionLocal() as db:
            u = ensure_user(db, chat_id, lang_code)
            tg_send_message(chat_id, t("voice_only", ui_of(u)), reply_markup=user_keyboard(u))


def _handle_callback(cq: Dict[str, Any]) -> None:
    data = cq.get("data", "")
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    cq_id = cq["id"]
    lang_code = (cq.get("from") or {}).get("language_code", "")

    def show_main():
        tg_edit_message(chat_id, message_id, format_status_text(u), reply_markup=build_main_keyboard(u))

    with SessionLocal() as db:
        u = ensure_user(db, chat_id, lang_code)
        ui = ui_of(u)

        if data == "lang:menu":
            # Открыть список языков (редактируем текущее сообщение)
            tg_edit_message(chat_id, message_id, t("choose_lang", ui), reply_markup=build_lang_keyboard(u))

        elif data.startswith("lang:set:"):
            code = data.split(":", 2)[2]
            if code in SUPPORTED_LANG_CODES:
                u.target_lang = code
                u.mode = "translate"
                u.conversation_source_lang = None
                u.conversation_target_lang = None
                db.commit()
            show_main()

        elif data == "menu:main":
            show_main()

        elif data == "mode:conversation":
            u.mode = "conversation"
            # НЕ сбрасываем уже настроенную пару — удобнее для пользователя
            db.commit()
            show_main()

        elif data == "conversation:reset":
            u.conversation_source_lang = None
            u.conversation_target_lang = None
            db.commit()
            show_main()

        elif data == "buy:menu":
            tg_edit_message(chat_id, message_id, t("choose_package", ui), reply_markup=build_packages_keyboard(u))

        elif data == "help:menu":
            tg_edit_message(chat_id, message_id, t("help_text", ui), reply_markup=build_main_keyboard(u))

        elif data == "support:menu":
            tg_edit_message(chat_id, message_id, t("support_prompt", ui), reply_markup=build_main_keyboard(u))

        elif data.startswith("paddle:"):
            # Ссылку на оплату шлём отдельным сообщением, меню оставляем на месте
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


# =========================================================
# WhatsApp Cloud API — Webhook
# =========================================================

@app.get("/whatsapp/webhook")
async def whatsapp_webhook_verify(
    request: Request,
    hub_mode: str = None,
    hub_challenge: str = None,
    hub_verify_token: str = None,
):
    """Верификация webhook со стороны Meta."""
    # FastAPI не принимает алиасы с точкой напрямую, читаем из query вручную
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    challenge = params.get("hub.challenge")
    verify_token = params.get("hub.verify_token")

    if mode == "subscribe" and verify_token == WA_VERIFY_TOKEN:
        log.info("WhatsApp webhook verified successfully")
        return PlainTextResponse(challenge)

    log.warning(f"WhatsApp webhook verification failed: mode={mode}, token={verify_token}")
    return PlainTextResponse("Forbidden", status_code=403)


@app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    """Приём входящих сообщений от WhatsApp."""
    try:
        data = await request.json()
    except Exception:
        return PlainTextResponse("bad json", status_code=400)

    log.info(f"WhatsApp webhook incoming: {json.dumps(data, ensure_ascii=False)}")

    try:
        entries = data.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                for message in messages:
                    wa_phone = message.get("from")
                    msg_type = message.get("type")
                    log.info(f"WhatsApp message from {wa_phone}: type={msg_type}")
                    wa_handle_message(wa_phone, message)
    except Exception as e:
        log.error(f"WhatsApp webhook processing error: {e}")

    return PlainTextResponse("ok")


# =========================================================
# WhatsApp — helpers и логика
# =========================================================

def wa_send_text(to: str, text: str) -> None:
    """Отправить текстовое сообщение."""
    if not WA_TOKEN or not WA_PHONE_ID:
        log.warning("WA_TOKEN or WA_PHONE_ID not set, skipping wa_send_text")
        return
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    resp = requests.post(
        WA_API_URL,
        headers={"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        log.error(f"wa_send_text error {resp.status_code}: {resp.text}")


def wa_send_buttons(to: str, body: str, buttons: list) -> None:
    """Отправить сообщение с Reply Buttons (макс. 3 кнопки, 20 символов каждая)."""
    if not WA_TOKEN or not WA_PHONE_ID:
        log.warning("WA_TOKEN or WA_PHONE_ID not set, skipping wa_send_buttons")
        return
    # buttons = [{"id": "btn_id", "title": "Button text"}, ...]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                    for b in buttons[:3]
                ]
            },
        },
    }
    resp = requests.post(
        WA_API_URL,
        headers={"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        log.error(f"wa_send_buttons error {resp.status_code}: {resp.text}")


def wa_download_audio(media_id: str) -> bytes:
    """Скачать аудиофайл с серверов Meta по media_id."""
    # Шаг 1 — получаем URL файла
    url_resp = requests.get(
        f"https://graph.facebook.com/v19.0/{media_id}",
        headers={"Authorization": f"Bearer {WA_TOKEN}"},
        timeout=REQUEST_TIMEOUT,
    )
    if not url_resp.ok:
        raise RuntimeError(f"wa_download_audio: get url failed {url_resp.status_code}: {url_resp.text[:200]}")
    download_url = url_resp.json().get("url")
    if not download_url:
        raise RuntimeError("wa_download_audio: no url in response")

    # Шаг 2 — скачиваем сам файл
    file_resp = requests.get(
        download_url,
        headers={"Authorization": f"Bearer {WA_TOKEN}"},
        timeout=REQUEST_TIMEOUT,
    )
    if not file_resp.ok:
        raise RuntimeError(f"wa_download_audio: download failed {file_resp.status_code}")
    return file_resp.content


def wa_send_audio(to: str, audio_bytes: bytes) -> None:
    """Отправить голосовое сообщение (ogg/opus) в WhatsApp через upload + send."""
    if not WA_TOKEN or not WA_PHONE_ID:
        log.warning("WA_TOKEN or WA_PHONE_ID not set, skipping wa_send_audio")
        return

    # Шаг 1 — загрузить аудио на сервера Meta
    upload_resp = requests.post(
        f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/media",
        headers={"Authorization": f"Bearer {WA_TOKEN}"},
        files={"file": ("voice.ogg", audio_bytes, "audio/ogg; codecs=opus")},
        data={"messaging_product": "whatsapp"},
        timeout=REQUEST_TIMEOUT,
    )
    if not upload_resp.ok:
        raise RuntimeError(f"wa_send_audio: upload failed {upload_resp.status_code}: {upload_resp.text[:200]}")
    media_id = upload_resp.json().get("id")
    if not media_id:
        raise RuntimeError("wa_send_audio: no media_id in upload response")

    # Шаг 2 — отправить как voice (не audio — иначе не будет иконки микрофона)
    send_resp = requests.post(
        WA_API_URL,
        headers={"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "audio",
            "audio": {"id": media_id},
        },
        timeout=REQUEST_TIMEOUT,
    )
    if not send_resp.ok:
        log.error(f"wa_send_audio: send failed {send_resp.status_code}: {send_resp.text[:200]}")


def wa_process_voice(wa_phone: str, message: dict) -> None:
    """Обработка входящего голосового сообщения от WhatsApp-пользователя."""
    with SessionLocal() as db:
        # Получаем или создаём пользователя по wa_phone
        user = db.query(User).filter(User.wa_phone == wa_phone).first()
        if not user:
            # telegram_id — PK NOT NULL, для WA используем отрицательный хэш от номера
            fake_tg_id = -(abs(hash(wa_phone)) % (10 ** 15))
            user = User(
                telegram_id=fake_tg_id,
                wa_phone=wa_phone,
                platform="wa",
                trial_left=TRIAL_LIMIT,
                trial_messages=0,
                balance_seconds=0,
                ui_lang="en",
                target_lang="ru",  # дефолт — переводить на русский
                mode="translate",
            )
            db.add(user)
            db.commit()
            db.refresh(user)

        # Биллинг: duration может отсутствовать в WA, ставим 0 — спишем по факту
        duration = int(message.get("audio", {}).get("duration") or 0)
        b_mode, charge = decide_billing(user, duration)
        if b_mode == "deny":
            wa_send_text(wa_phone, "⚠️ No balance left. Send *buy* to purchase minutes.")
            wa_send_main_menu(wa_phone)
            return

        # Скачиваем аудио
        media_id = message.get("audio", {}).get("id")
        if not media_id:
            wa_send_text(wa_phone, "❌ Could not get audio file. Please try again.")
            return

        try:
            audio_bytes = wa_download_audio(media_id)
        except Exception as e:
            log.error(f"wa_process_voice: download error: {e}")
            wa_send_text(wa_phone, "❌ Could not download audio. Please try again.")
            return

        # STT — транскрибируем
        try:
            transcript, ok = transcribe_with_fallback(audio_bytes, 0)
        except Exception as e:
            log.error(f"wa_process_voice: STT error: {e}")
            wa_send_text(wa_phone, "❌ Could not recognize speech. Please try again.")
            return

        if not transcript:
            wa_send_text(wa_phone, "❌ Empty voice message. Please try again.")
            return

        if not ok:
            wa_send_text(wa_phone, "🔇 Could not understand the audio clearly. Please try again in a quieter environment.")
            return

        log.info(f"WA voice from {wa_phone}: transcript={transcript!r}")

        # Определяем язык входящего и переводим
        source_lang = detect_language_name(transcript)
        target_lang = lang_name(user.target_lang)
        try:
            translated = openai_translate_text(transcript, target_lang, source_lang=source_lang)
        except Exception as e:
            log.error(f"wa_process_voice: translate error: {e}")
            wa_send_text(wa_phone, "❌ Translation error. Please try again.")
            return

        log.info(f"WA voice from {wa_phone}: translated={translated!r}")

        # TTS — синтезируем голос (opus — сразу подходит для WhatsApp ogg)
        try:
            tts_bytes = openai_tts(translated)
        except Exception as e:
            log.error(f"wa_process_voice: TTS error: {e}")
            # Отправляем хотя бы текст если TTS упал
            wa_send_text(wa_phone, f"📝 {transcript}\n➡️ {translated}")
            return

        # Биллинг
        apply_billing(db, user, b_mode, charge)

        # Отправляем результат: сначала текст-лог, потом голосовое
        balance_left = max(0, int(user.balance_seconds or 0)) // 60
        wa_send_text(
            wa_phone,
            f"📝 *{source_lang}:* {transcript}\n"
            f"➡️ *{target_lang}:* {translated}\n\n"
            f"⏱ Balance: {balance_left} min"
        )
        try:
            wa_send_audio(wa_phone, tts_bytes)
        except Exception as e:
            log.error(f"wa_process_voice: send audio error: {e}")

        # Показываем кнопки после перевода
        wa_send_main_menu(wa_phone)


def wa_handle_message(wa_phone: str, message: dict) -> None:
    """Основная точка входа для обработки входящего сообщения WhatsApp."""
    msg_type = message.get("type")

    # Нажатие на Reply Button
    if msg_type == "interactive":
        interactive = message.get("interactive", {})
        if interactive.get("type") == "button_reply":
            button_id = interactive["button_reply"]["id"]
            wa_handle_button(wa_phone, button_id)
        return

    # Текстовое сообщение — показываем главный экран
    if msg_type == "text":
        wa_send_main_menu(wa_phone)
        return

    # Голосовое сообщение
    if msg_type == "audio":
        wa_process_voice(wa_phone, message)
        return

    # Всё остальное игнорируем
    log.info(f"WhatsApp: unsupported message type {msg_type} from {wa_phone}")


def wa_send_main_menu(wa_phone: str) -> None:
    """Отправить главный экран с тремя кнопками."""
    wa_send_buttons(
        to=wa_phone,
        body=(
            "👋 Welcome to *LingoVox* — voice translator!\n\n"
            "Send me a voice message and I'll translate it.\n"
            "Or choose an option below:"
        ),
        buttons=[
            {"id": "btn_meeting", "title": "👥 Live Meeting"},
            {"id": "btn_language", "title": "🌍 Language"},
            {"id": "btn_help",    "title": "ℹ️ Help"},
        ],
    )


def wa_handle_button(wa_phone: str, button_id: str) -> None:
    """Обработка нажатия кнопки."""
    if button_id == "btn_meeting":
        wa_send_buttons(
            to=wa_phone,
            body=(
                "👥 *Live Meeting mode*\n\n"
                "Speak in turns — I'll translate both sides in real time.\n"
                "Georgian 🇬🇪, Kazakh 🇰🇿, Uzbek 🇺🇿 voice supported!\n\n"
                "Send a voice message to start."
            ),
            buttons=[
                {"id": "btn_stop",    "title": "⏹ Stop Meeting"},
                {"id": "btn_swap",    "title": "🔄 Swap langs"},
                {"id": "btn_back",    "title": "← Back"},
            ],
        )

    elif button_id == "btn_language":
        wa_send_text(
            wa_phone,
            "🌍 *Choose your language*\n\n"
            "Reply with the name of the language you want to translate *to*:\n\n"
            "• English\n• Russian\n• Georgian\n• Kazakh\n• Uzbek\n"
            "• Turkish\n• Spanish\n• Arabic\n• Hindi\n• Portuguese\n\n"
            "Example: just type *Georgian*"
        )

    elif button_id == "btn_help":
        wa_send_text(
            wa_phone,
            "ℹ️ *How LingoVox works*\n\n"
            "1️⃣ Send a voice message — I'll detect the language and translate it\n"
            "2️⃣ You'll get back a translated voice + text\n"
            "3️⃣ Forward the voice reply to your contact\n\n"
            "🎙 *Live Meeting mode* — place your phone on the table, "
            "take turns speaking — I translate both sides in real time\n\n"
            "🇬🇪 Georgian • 🇰🇿 Kazakh • 🇺🇿 Uzbek voice supported "
            "(Google Translate doesn't support these!)\n\n"
            "Questions? Contact @arkhipov_stepan"
        )

    elif button_id in ("btn_stop", "btn_back"):
        wa_send_main_menu(wa_phone)

    elif button_id == "btn_swap":
        wa_send_text(wa_phone, "🔄 Languages swapped! Send a voice message to continue.")

    else:
        log.warning(f"WhatsApp: unknown button_id={button_id} from {wa_phone}")
