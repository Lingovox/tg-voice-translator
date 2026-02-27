import os
import time
import jwt
import requests
from fastapi import FastAPI, Request
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, DateTime, func
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import NoResultFound

# =========================
# ENV
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY")
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID")
CRYPTOCLOUD_SECRET_KEY = os.getenv("CRYPTOCLOUD_SECRET_KEY")
BASE_URL = os.getenv("BASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")

# =========================
# DATABASE
# =========================

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True)
    target_lang = Column(String(10), default="en")
    trial_messages = Column(Integer, default=5)
    balance_seconds = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger)
    order_id = Column(String(64))
    invoice_id = Column(String(64))
    package_code = Column(String(16))
    amount_usd = Column(Integer)
    status = Column(String(32), default="created")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

# =========================
# APP
# =========================

app = FastAPI()

# =========================
# PACKAGES
# =========================

PACKAGES = {
    "P30": {"minutes": 30, "price": 3},
    "P60": {"minutes": 60, "price": 8},
    "P180": {"minutes": 180, "price": 20},
    "P600": {"minutes": 600, "price": 50},
}

# =========================
# UTIL
# =========================

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

def _is_paid_status(status: str) -> bool:
    if not status:
        return False
    s = status.lower()
    return (
        "paid" in s
        or "success" in s
        or s in ["paid", "success", "completed"]
    )

def verify_token(token: str):
    return jwt.decode(token, CRYPTOCLOUD_SECRET_KEY, algorithms=["HS256"])

# =========================
# ROOT
# =========================

@app.get("/")
def root():
    return {"status": "ok"}

# =========================
# TELEGRAM WEBHOOK
# =========================

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    if "message" not in data:
        return {"ok": True}

    message = data["message"]
    chat_id = message["chat"]["id"]

    db = SessionLocal()
    user = db.query(User).filter_by(telegram_id=chat_id).first()

    if not user:
        user = User(telegram_id=chat_id)
        db.add(user)
        db.commit()
        db.refresh(user)

    if message.get("text") == "/start":
        send_message(chat_id, f"🎁 У вас {user.trial_messages} бесплатных сообщений.\n\nКупить минуты: /buy")
        return {"ok": True}

    if message.get("text") == "/buy":
        send_message(chat_id, "Выберите пакет:\nP30 - 3$\nP60 - 8$\nP180 - 20$\nP600 - 50$")
        return {"ok": True}

    return {"ok": True}

# =========================
# CREATE INVOICE
# =========================

@app.post("/create_invoice/{package_code}")
def create_invoice(package_code: str, telegram_id: int):
    if not CRYPTOCLOUD_API_KEY or not CRYPTOCLOUD_SHOP_ID:
        return {"error": "CryptoCloud env vars missing"}

    package_code = package_code.upper()
    if package_code not in PACKAGES:
        return {"error": "Invalid package"}

    package = PACKAGES[package_code]
    order_id = f"{telegram_id}_{package_code}_{int(time.time())}"

    payload = {
        "amount": package["price"],
        "currency": "USD",
        "shop_id": CRYPTOCLOUD_SHOP_ID,
        "order_id": order_id,
    }

    headers = {
        "Authorization": f"Token {CRYPTOCLOUD_API_KEY}"
    }

    response = requests.post(
        "https://api.cryptocloud.plus/v2/invoice/create",
        json=payload,
        headers=headers
    )

    data = response.json()

    db = SessionLocal()
    payment = Payment(
        telegram_id=telegram_id,
        order_id=order_id,
        invoice_id=data.get("uuid", ""),
        package_code=package_code,
        amount_usd=package["price"],
        status="created"
    )
    db.add(payment)
    db.commit()

    return data

# =========================
# POSTBACK
# =========================

@app.post("/payments/cryptocloud/postback")
async def cryptocloud_postback(request: Request):
    raw = await request.body()
    print("==== RAW POSTBACK ====")
    print(raw.decode("utf-8", "ignore")[:4000])

    payload = await request.json()

    # Берём статус и order_id из payload / invoice_info
    invoice_info = payload.get("invoice_info") or {}
    order_id = payload.get("order_id") or invoice_info.get("order_id")
    invoice_id = (invoice_info.get("uuid") or payload.get("invoice_id") or payload.get("uuid") or "")

    # Статусы: payload.status = success, invoice_info.status = paid
    status = (
        invoice_info.get("status")
        or invoice_info.get("invoice_status")
        or payload.get("status")
        or payload.get("invoice_status")
    )

    def is_paid(s: str) -> bool:
        if not s:
            return False
        s = s.lower()
        return s in {"paid", "success", "completed"} or ("paid" in s) or ("success" in s)

    if not order_id:
        print("No order_id -> ignored")
        return {"ok": True}

    if not is_paid(status):
        print("Status not paid:", status)
        return {"ok": True}

    # ---- Обновляем payment и начисляем минуты ----
    db = SessionLocal()
    try:
        pay = db.query(Payment).filter(Payment.order_id == order_id).first()
        if not pay:
            print("Payment not found for order_id:", order_id)
            return {"ok": True}

        # идемпотентность: не начисляем повторно
        if (pay.status or "").lower() == "paid":
            print("Already paid, skip")
            return {"ok": True}

        pay.status = "paid"
        if invoice_id:
            pay.invoice_id = str(invoice_id)

        package_code = (pay.package_code or "").upper()
        minutes = PACKAGES.get(package_code, {}).get("minutes")
        if not minutes:
            print("Unknown package:", package_code)
            db.commit()
            return {"ok": True}

        user = db.query(User).filter(User.telegram_id == pay.telegram_id).first()
        if not user:
            user = User(telegram_id=pay.telegram_id)
            db.add(user)
            db.flush()

        user.balance_seconds = (user.balance_seconds or 0) + minutes * 60

        db.commit()

        send_message(user.telegram_id, f"✅ Оплата подтверждена.\nНачислено: +{minutes} мин.\nБаланс: {user.balance_seconds//60} мин.")
        print("✅ Credited minutes:", minutes, "to", user.telegram_id)

    finally:
        db.close()

    return {"ok": True}
