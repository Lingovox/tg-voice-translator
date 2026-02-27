# app.py
# Telegram Voice Translator + CryptoCloud minutes top-up (Render + FastAPI)

import base64
import datetime as dt
import hmac
import hashlib
import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    create_engine,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from openai import OpenAI


# -------------------------
# Config
# -------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")
CRYPTOCLOUD_API_KEY = os.environ.get("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.environ.get("CRYPTOCLOUD_SHOP_ID", "").strip()
CRYPTOCLOUD_SECRET_KEY = os.environ.get("CRYPTOCLOUD_SECRET_KEY", "").strip()

TRIAL_LIMIT = int(os.environ.get("TRIAL_LIMIT", "5") or "5")
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()

CRYPTOCLOUD_API_BASE = os.environ.get("CRYPTOCLOUD_API_BASE", "https://api.cryptocloud.plus").rstrip("/")

TRIAL_MAX_SECONDS_PER_MESSAGE = 60

PACKAGES = {
    "P30": {"usd": 3, "minutes": 30},
    "P60": {"usd": 8, "minutes": 60},
    "P180": {"usd": 20, "minutes": 180},
    "P600": {"usd": 50, "minutes": 600},
}

# Французский вернул, узбекский убрал
LANGS = [
    ("English", "en"),
    ("Русский", "ru"),
    ("Deutsch", "de"),
    ("Español", "es"),
    ("ไทย", "th"),
    ("Tiếng Việt", "vi"),
    ("Français", "fr"),
    ("Türkçe", "tr"),
]


# -------------------------
# FastAPI
# -------------------------
app = FastAPI()


# -------------------------
# DB
# -------------------------
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True, index=True)
    target_lang = Column(String, nullable=False, default="fr")

    trial_left = Column(Integer, nullable=False, default=TRIAL_LIMIT)
    # legacy column from earlier versions
    trial_messages = Column(Integer, nullable=False, default=TRIAL_LIMIT)

    is_subscribed = Column(Boolean, nullable=False, default=False)
    balance_seconds = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)

    order_id = Column(String, nullable=False, unique=True, index=True)
    # ВАЖНО: у тебя invoice_id NOT NULL -> значит мы обязаны вставлять его сразу
    invoice_id = Column(String, nullable=False, index=True)

    package_code = Column(String, nullable=False)
    amount_usd = Column(Integer, nullable=False)

    status = Column(String, nullable=False, default="created")
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


def ensure_schema() -> None:
    Base.metadata.create_all(engine)

    # add missing columns if earlier DB was incomplete
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS target_lang varchar DEFAULT 'fr'"))
        conn.execute(text(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_left integer DEFAULT {TRIAL_LIMIT}"))
        conn.execute(text(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_messages integer DEFAULT {TRIAL_LIMIT}"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_subscribed boolean DEFAULT false"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS balance_seconds integer DEFAULT 0"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at timestamp without time zone DEFAULT NOW()"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at timestamp without time zone DEFAULT NOW()"))

        conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS telegram_id bigint"))
        conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS order_id varchar"))
        conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS invoice_id varchar"))
        conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS package_code varchar"))
        conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS amount_usd integer DEFAULT 0"))
        conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS status varchar DEFAULT 'created'"))
        conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS created_at timestamp without time zone DEFAULT NOW()"))
        conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS updated_at timestamp without time zone DEFAULT NOW()"))


ensure_schema()


# -------------------------
# OpenAI
# -------------------------
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing")
client = OpenAI(api_key=OPENAI_API_KEY)


# -------------------------
# Telegram helpers
# -------------------------
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def tg_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{TG_API}/{method}"
    r = requests.post(url, json=payload, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "raw": r.text, "status_code": r.status_code}


def send_message(chat_id: int, text_: str, reply_markup: Optional[dict] = None) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text_}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_request("sendMessage", payload)


def answer_callback(callback_query_id: str, text_: str = "", show_alert: bool = False) -> None:
    payload = {"callback_query_id": callback_query_id}
    if text_:
        payload["text"] = text_
    if show_alert:
        payload["show_alert"] = True
    tg_request("answerCallbackQuery", payload)


def build_home_keyboard(user_lang: str) -> dict:
    rows = []
    for i in range(0, len(LANGS), 2):
        pair = LANGS[i : i + 2]
        row = []
        for label, code in pair:
            prefix = "✅ " if code == user_lang else ""
            row.append({"text": f"{prefix}{label}", "callback_data": f"lang_{code}"})
        rows.append(row)
    rows.append([{"text": "💳 Купить минуты", "callback_data": "open_buy"}])
    return {"inline_keyboard": rows}


def build_buy_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "30 мин — $3", "callback_data": "buy_P30"}],
            [{"text": "60 мин — $8", "callback_data": "buy_P60"}],
            [{"text": "180 мин — $20", "callback_data": "buy_P180"}],
            [{"text": "600 мин — $50", "callback_data": "buy_P600"}],
            [{"text": "⬅️ Назад", "callback_data": "back_home"}],
        ]
    }


def fmt_balance_minutes(balance_seconds: int) -> int:
    return max(0, int(balance_seconds or 0) // 60)


def get_or_create_user(db, telegram_id: int) -> User:
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if user:
        if user.target_lang is None:
            user.target_lang = "fr"
        if user.trial_left is None:
            user.trial_left = TRIAL_LIMIT
        if user.trial_messages is None:
            user.trial_messages = user.trial_left
        if user.balance_seconds is None:
            user.balance_seconds = 0
        db.commit()
        return user

    user = User(
        telegram_id=telegram_id,
        target_lang="fr",
        trial_left=TRIAL_LIMIT,
        trial_messages=TRIAL_LIMIT,
        balance_seconds=0,
        is_subscribed=False,
    )
    db.add(user)
    db.commit()
    return user


def show_home(chat_id: int, user: User) -> None:
    text_ = (
        "🎙 Голосовой переводчик\n\n"
        f"🌍 Язык перевода: {user.target_lang}\n"
        f"🎁 Бесплатных переводов: {user.trial_left} (≤ {TRIAL_MAX_SECONDS_PER_MESSAGE} сек)\n"
        f"💳 Баланс: {fmt_balance_minutes(user.balance_seconds)} мин\n\n"
        "Запиши голосовое — я переведу и пришлю озвучку."
    )
    send_message(chat_id, text_, reply_markup=build_home_keyboard(user.target_lang))


def telegram_get_file_bytes(file_id: str) -> Tuple[bytes, str]:
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"getFile failed: {j}")
    file_path = j["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    data = requests.get(file_url, timeout=60).content
    filename = file_path.split("/")[-1]
    return data, filename


# -------------------------
# JWT verify (HS256)
# -------------------------
def b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("utf-8"))


def verify_jwt_hs256(token: str, secret: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Bad JWT format")
    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    sig = b64url_decode(sig_b64)
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("JWT signature invalid")
    payload = json.loads(b64url_decode(payload_b64).decode("utf-8"))
    exp = payload.get("exp")
    if exp is not None and int(time.time()) > int(exp):
        raise ValueError("JWT expired")
    return payload


# -------------------------
# CryptoCloud
# -------------------------
def cryptocloud_headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CRYPTOCLOUD_API_KEY}",
        "X-Api-Key": CRYPTOCLOUD_API_KEY,
    }


def require_cryptocloud_env() -> Optional[str]:
    missing = []
    if not BASE_URL:
        missing.append("BASE_URL")
    if not CRYPTOCLOUD_API_KEY:
        missing.append("CRYPTOCLOUD_API_KEY")
    if not CRYPTOCLOUD_SHOP_ID:
        missing.append("CRYPTOCLOUD_SHOP_ID")
    if not CRYPTOCLOUD_SECRET_KEY:
        missing.append("CRYPTOCLOUD_SECRET_KEY")
    if missing:
        return " / ".join(missing)
    return None


def create_invoice(order_id: str, amount_usd: int) -> Tuple[str, str]:
    missing = require_cryptocloud_env()
    if missing:
        raise RuntimeError(f"CryptoCloud env vars missing: {missing}")

    notify_url = f"{BASE_URL}/payments/cryptocloud/postback"

    payload = {
        "shop_id": CRYPTOCLOUD_SHOP_ID,
        "amount": float(amount_usd),
        "currency": "USD",
        "order_id": order_id,
        "success_url": BASE_URL,
        "fail_url": BASE_URL,
        "notify_url": notify_url,
    }

    endpoints = [
        f"{CRYPTOCLOUD_API_BASE}/api/v2/invoice/create",
        f"{CRYPTOCLOUD_API_BASE}/api/v1/invoice/create",
    ]

    last_err = None
    for url in endpoints:
        try:
            r = requests.post(url, headers=cryptocloud_headers(), json=payload, timeout=30)
            data = r.json() if "application/json" in r.headers.get("content-type", "") else {"raw": r.text}
            if r.status_code >= 400:
                last_err = f"{url} -> HTTP {r.status_code}: {data}"
                continue

            invoice_uuid = (
                (data.get("result") or {}).get("uuid")
                or data.get("uuid")
                or (data.get("invoice") or {}).get("uuid")
            )
            pay_url = (
                (data.get("result") or {}).get("link")
                or data.get("link")
                or (data.get("result") or {}).get("pay_url")
                or data.get("pay_url")
                or (data.get("invoice") or {}).get("link")
            )
            if invoice_uuid and pay_url:
                return invoice_uuid, pay_url

            last_err = f"{url} -> unexpected response: {data}"
        except Exception as e:
            last_err = f"{url} -> exception: {e}"

    raise RuntimeError(f"CryptoCloud create invoice failed: {last_err}")


def credit_user_minutes(db, telegram_id: int, package_code: str) -> int:
    pack = PACKAGES.get(package_code)
    if not pack:
        raise ValueError("Unknown package_code")
    add_seconds = int(pack["minutes"]) * 60
    user = get_or_create_user(db, telegram_id)
    user.balance_seconds = int(user.balance_seconds or 0) + add_seconds
    db.commit()
    return add_seconds


# -------------------------
# OpenAI: voice pipeline
# -------------------------
def openai_transcribe(audio_bytes: bytes, filename: str) -> str:
    tr = client.audio.transcriptions.create(
        model="whisper-1",
        file=(filename, audio_bytes),
    )
    return tr.text or ""


def openai_translate_text(text_: str, target_lang: str) -> str:
    prompt = (
        "You are a professional translator.\n"
        f"Translate the text into the target language: {target_lang}.\n"
        "Return ONLY the translated text.\n\n"
        f"TEXT:\n{text_}\n"
    )
    resp = client.responses.create(model="gpt-4o-mini", input=prompt)
    out = []
    for item in resp.output or []:
        if item.type == "message":
            for c in item.content or []:
                if c.type == "output_text":
                    out.append(c.text)
    return "\n".join(out).strip()


def openai_tts(text_: str) -> Optional[bytes]:
    try:
        audio = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text_,
        )
        return audio.read()
    except Exception:
        return None


def telegram_send_voice(chat_id: int, audio_bytes: bytes, filename: str = "translation.mp3") -> None:
    url = f"{TG_API}/sendVoice"
    files = {"voice": (filename, audio_bytes)}
    data = {"chat_id": str(chat_id)}
    requests.post(url, data=data, files=files, timeout=60)


# -------------------------
# Routes
# -------------------------
@app.get("/")
def root():
    return {"ok": True, "service": "tg-voice-translator"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()

    try:
        with SessionLocal() as db:
            # MESSAGE
            if "message" in update:
                msg = update["message"]
                chat_id = msg["chat"]["id"]
                user = get_or_create_user(db, chat_id)

                text_msg = (msg.get("text") or "").strip()

                if text_msg == "/start":
                    show_home(chat_id, user)
                    return {"ok": True}

                if text_msg == "/buy":
                    send_message(chat_id, "💳 Выберите пакет минут:", reply_markup=build_buy_keyboard())
                    return {"ok": True}

                if text_msg == "/stats":
                    if ADMIN_ID and str(chat_id) != str(ADMIN_ID):
                        send_message(chat_id, "⛔️ Доступ запрещён.")
                        return {"ok": True}

                    users_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar() or 0
                    paid_count = db.execute(text("SELECT COUNT(*) FROM payments WHERE status='paid'")).scalar() or 0
                    created_count = db.execute(text("SELECT COUNT(*) FROM payments WHERE status='created'")).scalar() or 0
                    send_message(
                        chat_id,
                        f"📊 Stats\n\nUsers: {users_count}\nPayments paid: {paid_count}\nPayments created: {created_count}\n",
                    )
                    return {"ok": True}

                if "voice" in msg:
                    voice = msg["voice"]
                    duration = int(voice.get("duration") or 0)
                    file_id = voice["file_id"]

                    user = get_or_create_user(db, chat_id)
                    has_paid_balance = int(user.balance_seconds or 0) > 0

                    # balance/trial rules
                    if has_paid_balance:
                        deduct = max(1, duration)
                        if user.balance_seconds < deduct:
                            send_message(
                                chat_id,
                                "💳 На балансе недостаточно минут. Нажми «Купить минуты».",
                                reply_markup=build_home_keyboard(user.target_lang),
                            )
                            return {"ok": True}
                        user.balance_seconds -= deduct
                        db.commit()
                    else:
                        if user.trial_left <= 0:
                            send_message(
                                chat_id,
                                "🎁 Бесплатные переводы закончились. Нажми «Купить минуты».",
                                reply_markup=build_home_keyboard(user.target_lang),
                            )
                            return {"ok": True}
                        if duration > TRIAL_MAX_SECONDS_PER_MESSAGE:
                            send_message(
                                chat_id,
                                f"🎁 В бесплатном режиме можно до {TRIAL_MAX_SECONDS_PER_MESSAGE} сек.\n"
                                "Нажми «Купить минуты» для длинных переводов.",
                                reply_markup=build_home_keyboard(user.target_lang),
                            )
                            return {"ok": True}

                        user.trial_left -= 1
                        user.trial_messages = user.trial_left
                        db.commit()

                    send_message(chat_id, "⏳ Слушаю и перевожу...")
                    audio_bytes, filename = telegram_get_file_bytes(file_id)

                    src_text = openai_transcribe(audio_bytes, filename)
                    if not src_text.strip():
                        send_message(chat_id, "Не удалось распознать речь. Попробуй ещё раз.")
                        show_home(chat_id, user)
                        return {"ok": True}

                    translated = openai_translate_text(src_text, user.target_lang)
                    if not translated.strip():
                        send_message(chat_id, "Не удалось перевести. Попробуй ещё раз.")
                        show_home(chat_id, user)
                        return {"ok": True}

                    tts_bytes = openai_tts(translated)
                    if tts_bytes:
                        telegram_send_voice(chat_id, tts_bytes)
                    else:
                        send_message(chat_id, translated)

                    user = get_or_create_user(db, chat_id)
                    show_home(chat_id, user)
                    return {"ok": True}

                # default
                show_home(chat_id, user)
                return {"ok": True}

            # CALLBACKS
            if "callback_query" in update:
                cq = update["callback_query"]
                cb_id = cq["id"]
                cb_data = cq.get("data") or ""
                chat_id = cq["message"]["chat"]["id"]
                user = get_or_create_user(db, chat_id)

                if cb_data.startswith("lang_"):
                    code = cb_data.replace("lang_", "").strip()
                    allowed = {c for _, c in LANGS}
                    if code in allowed:
                        user.target_lang = code
                        db.commit()
                    answer_callback(cb_id)
                    show_home(chat_id, user)  # возвращаем интерфейс
                    return {"ok": True}

                if cb_data == "open_buy":
                    answer_callback(cb_id)
                    send_message(chat_id, "💳 Выберите пакет минут:", reply_markup=build_buy_keyboard())
                    return {"ok": True}

                if cb_data == "back_home":
                    answer_callback(cb_id)
                    show_home(chat_id, user)
                    return {"ok": True}

                if cb_data.startswith("buy_"):
                    package_code = cb_data.replace("buy_", "").strip()
                    if package_code not in PACKAGES:
                        answer_callback(cb_id, "Неизвестный пакет", show_alert=True)
                        return {"ok": True}

                    # ВАЖНО: invoice_id NOT NULL -> создаём инвойс ДО записи в БД
                    order_id = f"{chat_id}_{package_code}_{int(time.time())}"
                    amount_usd = int(PACKAGES[package_code]["usd"])

                    try:
                        invoice_uuid, pay_url = create_invoice(order_id, amount_usd)

                        # Теперь пишем в БД уже с invoice_id
                        p = Payment(
                            telegram_id=chat_id,
                            order_id=order_id,
                            invoice_id=invoice_uuid,
                            package_code=package_code,
                            amount_usd=amount_usd,
                            status="created",
                        )
                        db.add(p)
                        db.commit()

                        answer_callback(cb_id)
                        send_message(
                            chat_id,
                            f"✅ Счёт создан.\n\nПакет: {package_code}\nСумма: ${amount_usd}\n\n"
                            f"Оплата по ссылке:\n{pay_url}\n\n"
                            "После оплаты минуты начислятся автоматически.",
                        )
                        return {"ok": True}

                    except Exception as e:
                        answer_callback(cb_id)
                        send_message(chat_id, f"Ошибка создания счёта: {e}")
                        return {"ok": True}

                answer_callback(cb_id)
                return {"ok": True}

    except Exception as e:
        print("Webhook error:", repr(e))

    return {"ok": True}


@app.post("/payments/cryptocloud/postback")
async def cryptocloud_postback(request: Request):
    raw = await request.body()
    print("==== RAW POSTBACK ====")
    print(raw.decode("utf-8", errors="replace"))

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=200)

    # verify token if present
    token = data.get("token")
    if token and CRYPTOCLOUD_SECRET_KEY:
        try:
            decoded = verify_jwt_hs256(token, CRYPTOCLOUD_SECRET_KEY)
            print("==== DECODED DATA ====")
            print(decoded)
        except Exception as e:
            print("Token verify failed:", repr(e))

    status = (data.get("status") or "").lower().strip()
    order_id = (data.get("order_id") or "").strip()

    invoice_info = data.get("invoice_info") or {}
    invoice_uuid = (invoice_info.get("uuid") or "").strip()
    invoice_status = (invoice_info.get("invoice_status") or "").lower().strip()

    paid_flag = invoice_status == "success" or status == "success"

    if not order_id:
        print("Missing order_id in postback")
        return JSONResponse({"ok": True}, status_code=200)

    with SessionLocal() as db:
        pay = db.query(Payment).filter(Payment.order_id == order_id).first()

        # fallback (если вдруг order_id не совпал)
        if not pay and invoice_uuid:
            pay = db.query(Payment).filter(Payment.invoice_id == invoice_uuid).first()

        if not pay:
            print("Payment not found:", order_id, invoice_uuid)
            return JSONResponse({"ok": True}, status_code=200)

        if pay.status == "paid":
            print("Already paid, skip")
            return JSONResponse({"ok": True}, status_code=200)

        if not paid_flag:
            print("Status not paid:", status, invoice_status)
            pay.updated_at = dt.datetime.utcnow()
            db.commit()
            return JSONResponse({"ok": True}, status_code=200)

        pay.status = "paid"
        db.commit()

        try:
            added_seconds = credit_user_minutes(db, int(pay.telegram_id), pay.package_code)
            added_min = added_seconds // 60
            send_message(
                int(pay.telegram_id),
                f"✅ Оплата подтверждена!\nНачислено: {added_min} мин\n\nПакет: {pay.package_code}",
            )
        except Exception as e:
            print("Credit error:", repr(e))
            send_message(int(pay.telegram_id), f"⚠️ Оплата получена, но не удалось начислить минуты: {e}")

    return JSONResponse({"ok": True}, status_code=200)


@app.get("/debug/env")
def debug_env():
    keys = [
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "BASE_URL",
        "CRYPTOCLOUD_API_KEY",
        "CRYPTOCLOUD_SHOP_ID",
        "CRYPTOCLOUD_SECRET_KEY",
        "TRIAL_LIMIT",
        "ADMIN_ID",
    ]
    present = {k: bool(os.environ.get(k)) for k in keys}
    return {"present": present}
