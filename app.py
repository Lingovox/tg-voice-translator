import os
import time
import json
import requests
import jwt
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request
from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, DateTime, Boolean, func, text
)
from sqlalchemy.orm import sessionmaker, declarative_base


# =========================
# ENV
# =========================

def env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v

TELEGRAM_TOKEN = env_required("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # может быть нужен позже для перевода
DATABASE_URL = env_required("DATABASE_URL")

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY")
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID")
CRYPTOCLOUD_SECRET_KEY = os.getenv("CRYPTOCLOUD_SECRET_KEY")

BASE_URL = os.getenv("BASE_URL")  # https://tg-voice-translator-1.onrender.com
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TRIAL_LIMIT = int(os.getenv("TRIAL_LIMIT", "5"))  # маркетинг: 5 бесплатных сообщений (≤ 60 сек)


# =========================
# DB
# =========================

db_url = DATABASE_URL
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(db_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True)

    # язык назначения (кнопки)
    target_lang = Column(String(10), nullable=True, default="en")

    # старая логика могла вести trial_left (оставляем как есть)
    trial_left = Column(Integer, nullable=True, default=TRIAL_LIMIT)

    # признак подписки (оставляем)
    is_subscribed = Column(Boolean, nullable=True, default=False)

    # новая логика: счетчик бесплатных сообщений и баланс секунд
    trial_messages = Column(Integer, nullable=True, default=TRIAL_LIMIT)
    balance_seconds = Column(Integer, nullable=True, default=0)

    created_at = Column(DateTime, nullable=True, server_default=func.now())
    updated_at = Column(DateTime, nullable=True, onupdate=func.now())


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=False)
    order_id = Column(String(64), nullable=False)
    invoice_id = Column(String(64), nullable=True, default="")
    package_code = Column(String(16), nullable=False)
    amount_usd = Column(Integer, nullable=False)
    status = Column(String(32), nullable=True, default="created")

    created_at = Column(DateTime, nullable=True, server_default=func.now())
    updated_at = Column(DateTime, nullable=True, onupdate=func.now())


# =========================
# APP
# =========================

app = FastAPI()


# =========================
# CONFIG
# =========================

# Пакеты: $3 → 30 мин, $8 → 60 мин, $20 → 180 мин, $50 → 600 мин
PACKAGES = {
    "P30": {"minutes": 30, "price": 3},
    "P60": {"minutes": 60, "price": 8},
    "P180": {"minutes": 180, "price": 20},
    "P600": {"minutes": 600, "price": 50},
}

# Языки кнопок (вернули французский)
LANGS = {
    "en": "English",
    "ru": "Русский",
    "de": "Deutsch",
    "es": "Español",
    "fr": "Français",
    "th": "ไทย",
    "vi": "Tiếng Việt",
    "tr": "Türkçe",
}

# =========================
# TELEGRAM HELPERS
# =========================

def tg_api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"

def send_message(chat_id: int, text_msg: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    payload = {"chat_id": chat_id, "text": text_msg}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(tg_api("sendMessage"), json=payload, timeout=20)

def answer_callback(callback_query_id: str) -> None:
    requests.post(tg_api("answerCallbackQuery"), json={"callback_query_id": callback_query_id}, timeout=20)

def safe_int(x, default=0) -> int:
    try:
        return int(x)
    except Exception:
        return default


# =========================
# BUSINESS HELPERS
# =========================

def is_paid_status(status: Optional[str]) -> bool:
    if not status:
        return False
    s = str(status).lower()
    # CryptoCloud отдаёт success/paid в разных местах
    return (
        s == "paid"
        or s == "success"
        or s == "completed"
        or "paid" in s
        or "success" in s
    )

def require_cryptocloud_env() -> Optional[str]:
    missing = []
    for k in ("CRYPTOCLOUD_API_KEY", "CRYPTOCLOUD_SHOP_ID", "CRYPTOCLOUD_SECRET_KEY", "BASE_URL"):
        if not os.getenv(k):
            missing.append(k)
    return ", ".join(missing) if missing else None


# =========================
# ROOT
# =========================

@app.get("/")
def root():
    return {"status": "ok"}


# =========================
# UI BUILDERS
# =========================

def build_lang_keyboard() -> Dict[str, Any]:
    # две колонки
    items = list(LANGS.items())
    rows = []
    for i in range(0, len(items), 2):
        row = []
        for code, title in items[i:i+2]:
            row.append({"text": title, "callback_data": f"lang_{code}"})
        rows.append(row)
    return {"inline_keyboard": rows}

def build_buy_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "30 мин — $3", "callback_data": "buy_P30"}],
            [{"text": "60 мин — $8", "callback_data": "buy_P60"}],
            [{"text": "180 мин — $20", "callback_data": "buy_P180"}],
            [{"text": "600 мин — $50", "callback_data": "buy_P600"}],
        ]
    }


# =========================
# DB HELPERS
# =========================

def get_or_create_user(db, telegram_id: int) -> User:
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if user:
        # подстрахуем defaults
        if user.trial_messages is None:
            user.trial_messages = TRIAL_LIMIT
        if user.balance_seconds is None:
            user.balance_seconds = 0
        if not user.target_lang:
            user.target_lang = "en"
        db.commit()
        return user

    user = User(
        telegram_id=telegram_id,
        target_lang="en",
        trial_left=TRIAL_LIMIT,
        trial_messages=TRIAL_LIMIT,
        balance_seconds=0,
        is_subscribed=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# =========================
# TELEGRAM WEBHOOK
# =========================

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    # ==== callbacks (кнопки) ====
    if "callback_query" in data:
        q = data["callback_query"]
        cb_id = q["id"]
        chat_id = q["message"]["chat"]["id"]
        cb_data = q.get("data", "")

        db = SessionLocal()
        try:
            user = get_or_create_user(db, chat_id)

            # смена языка
            if cb_data.startswith("lang_"):
                code = cb_data.replace("lang_", "").strip()
                if code in LANGS:
                    user.target_lang = code
                    db.commit()
                    send_message(
                        chat_id,
                        f"✅ Язык перевода установлен: {LANGS[code]}",
                        reply_markup=build_buy_keyboard()
                    )
                else:
                    send_message(chat_id, "⚠️ Неизвестный язык.")
                answer_callback(cb_id)
                return {"ok": True}

            # покупка пакета
            if cb_data.startswith("buy_"):
                package_code = cb_data.replace("buy_", "").strip().upper()
                answer_callback(cb_id)

                if require_cryptocloud_env():
                    send_message(chat_id, f"⚠️ Оплата не настроена. Missing env: {require_cryptocloud_env()}")
                    return {"ok": True}

                resp = create_invoice_internal(package_code=package_code, telegram_id=chat_id)
                if not resp.get("ok"):
                    send_message(chat_id, f"❌ Ошибка создания счёта: {resp.get('error')}")
                    return {"ok": True}

                pay_url = resp.get("pay_url")
                inv = resp.get("invoice_id", "")
                send_message(
                    chat_id,
                    f"🧾 Счёт создан.\nInvoice: {inv}\n\nПерейдите к оплате:\n{pay_url}"
                )
                return {"ok": True}

            answer_callback(cb_id)
            return {"ok": True}

        finally:
            db.close()

    # ==== обычные сообщения ====
    msg = data.get("message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    text_msg = (msg.get("text") or "").strip()

    db = SessionLocal()
    try:
        user = get_or_create_user(db, chat_id)

        if text_msg == "/start":
            lang_title = LANGS.get(user.target_lang or "en", user.target_lang or "en")
            bal_min = (user.balance_seconds or 0) // 60
            free_left = user.trial_messages if user.trial_messages is not None else TRIAL_LIMIT

            send_message(
                chat_id,
                f"👋 Привет!\n"
                f"🌍 Текущий язык: {lang_title}\n"
                f"🎁 Free trial: {free_left} сообщений (≤ 1 мин каждое)\n"
                f"⏱ Баланс: {bal_min} мин\n\n"
                f"Выберите язык кнопками ниже:",
                reply_markup=build_lang_keyboard()
            )
            return {"ok": True}

        if text_msg in ("/buy", "Купить минуты"):
            send_message(chat_id, "💳 Выберите пакет минут:", reply_markup=build_buy_keyboard())
            return {"ok": True}

        if text_msg in ("/balance", "Баланс"):
            bal_min = (user.balance_seconds or 0) // 60
            free_left = user.trial_messages if user.trial_messages is not None else TRIAL_LIMIT
            send_message(chat_id, f"⏱ Баланс: {bal_min} мин\n🎁 Free trial: {free_left} сообщений")
            return {"ok": True}

        if text_msg == "/stats":
            if ADMIN_ID and chat_id != ADMIN_ID:
                send_message(chat_id, "⛔️ Нет доступа.")
                return {"ok": True}

            users_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar() or 0
            paid_count = db.execute(text("SELECT COUNT(*) FROM payments WHERE status='paid'")).scalar() or 0
            revenue = db.execute(text("SELECT COALESCE(SUM(amount_usd),0) FROM payments WHERE status='paid'")).scalar() or 0
            send_message(chat_id, f"📊 Stats\nUsers: {users_count}\nPaid: {paid_count}\nRevenue (USD): {revenue}")
            return {"ok": True}

        # Пока перевод голосом/текстом здесь не делаем (чтобы не ломать),
        # можно добавить позже. Сейчас — дружелюбный ответ:
        send_message(
            chat_id,
            "Я готов принимать оплату и вести баланс минут.\n"
            "Команды:\n"
            "/buy — купить минуты\n"
            "/balance — баланс\n"
            "/start — меню"
        )
        return {"ok": True}

    finally:
        db.close()


# =========================
# CREATE INVOICE (INTERNAL)
# =========================

def cryptocloud_create_invoice(amount_usd: int, order_id: str) -> Dict[str, Any]:
    """
    Возвращает:
    {
      "uuid": "INV-....",
      "link": "https://...."  (или url/pay_url)
    }
    """
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}"}
    payload = {
        "amount": float(amount_usd),
        "currency": "USD",
        "shop_id": CRYPTOCLOUD_SHOP_ID,
        "order_id": order_id,
    }

    # В документации CryptoCloud обычно /v2/invoice/create
    r = requests.post("https://api.cryptocloud.plus/v2/invoice/create", json=payload, headers=headers, timeout=30)
    try:
        j = r.json()
    except Exception:
        return {"error": f"Bad response from CryptoCloud: {r.status_code} {r.text[:200]}"}

    if not r.ok:
        return {"error": f"CryptoCloud error: {j}"}

    return j


def create_invoice_internal(package_code: str, telegram_id: int) -> Dict[str, Any]:
    package_code = (package_code or "").upper().strip()
    if package_code not in PACKAGES:
        return {"ok": False, "error": "Invalid package code"}

    missing = require_cryptocloud_env()
    if missing:
        return {"ok": False, "error": f"Missing env: {missing}"}

    pkg = PACKAGES[package_code]
    order_id = f"{telegram_id}_{package_code}_{int(time.time())}"

    cc = cryptocloud_create_invoice(amount_usd=pkg["price"], order_id=order_id)
    if cc.get("error"):
        return {"ok": False, "error": cc["error"]}

    # link может называться по-разному
    pay_url = cc.get("link") or cc.get("pay_url") or cc.get("url")
    invoice_uuid = cc.get("uuid") or ""

    if not pay_url:
        return {"ok": False, "error": f"No pay url in response: {cc}"}

    db = SessionLocal()
    try:
        p = Payment(
            telegram_id=telegram_id,
            order_id=order_id,
            invoice_id=str(invoice_uuid),
            package_code=package_code,
            amount_usd=int(pkg["price"]),
            status="created",
        )
        db.add(p)
        db.commit()
    finally:
        db.close()

    return {"ok": True, "pay_url": pay_url, "invoice_id": str(invoice_uuid), "order_id": order_id}


# (опционально) endpoint — если ты захочешь дергать его извне:
@app.post("/create_invoice/{package_code}")
def create_invoice_endpoint(package_code: str, telegram_id: int):
    return create_invoice_internal(package_code=package_code, telegram_id=telegram_id)


# =========================
# POSTBACK
# =========================

@app.post("/payments/cryptocloud/postback")
async def cryptocloud_postback(request: Request):
    raw = await request.body()
    raw_text = raw.decode("utf-8", "ignore")

    print("==== RAW POSTBACK ====")
    print(raw_text[:4000])

    # Парсим JSON
    try:
        payload = json.loads(raw_text)
    except Exception as e:
        print("postback json parse error:", e)
        return {"ok": True}

    # (Опционально) проверяем подпись токена JWT — но в token часто только id/exp.
    # Если ключ неверный — лучше НЕ начислять.
    token = payload.get("token")
    if CRYPTOCLOUD_SECRET_KEY and token:
        try:
            _ = jwt.decode(token, CRYPTOCLOUD_SECRET_KEY, algorithms=["HS256"])
        except Exception as e:
            print("JWT verify failed:", e)
            return {"ok": True}

    invoice_info = payload.get("invoice_info") or {}

    # ВАЖНО: берем данные из payload / invoice_info (а не из decoded token)
    order_id = payload.get("order_id") or invoice_info.get("order_id")
    # у тебя в invoice_info.uuid приходит INV-xxxx
    invoice_uuid = invoice_info.get("uuid") or payload.get("invoice_id") or payload.get("uuid") or ""

    status = (
        invoice_info.get("status")
        or invoice_info.get("invoice_status")
        or payload.get("status")
        or payload.get("invoice_status")
    )

    if not order_id:
        print("No order_id -> ignored")
        return {"ok": True}

    if not is_paid_status(status):
        print("Status not paid:", status)
        return {"ok": True}

    # Обновляем payment и начисляем баланс идемпотентно
    db = SessionLocal()
    try:
        pay = db.query(Payment).filter(Payment.order_id == order_id).first()
        if not pay:
            print("Payment not found for order_id:", order_id)
            return {"ok": True}

        if (pay.status or "").lower() == "paid":
            print("Already paid, skip")
            return {"ok": True}

        pkg_code = (pay.package_code or "").upper()
        minutes = PACKAGES.get(pkg_code, {}).get("minutes")
        if not minutes:
            print("Unknown package_code:", pkg_code)
            return {"ok": True}

        user = db.query(User).filter(User.telegram_id == pay.telegram_id).first()
        if not user:
            user = User(telegram_id=pay.telegram_id, target_lang="en", trial_left=TRIAL_LIMIT, trial_messages=TRIAL_LIMIT, balance_seconds=0)
            db.add(user)
            db.flush()

        # начисляем
        user.balance_seconds = (user.balance_seconds or 0) + minutes * 60

        # фиксируем payment
        pay.status = "paid"
        if invoice_uuid:
            pay.invoice_id = str(invoice_uuid)

        db.commit()

        bal_min = (user.balance_seconds or 0) // 60
        send_message(user.telegram_id, f"✅ Оплата подтверждена.\nНачислено: +{minutes} мин\nБаланс: {bal_min} мин")
        print("✅ Credited minutes:", minutes, "to", user.telegram_id)

        return {"ok": True}

    finally:
        db.close()
