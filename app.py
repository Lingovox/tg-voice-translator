import os
import time
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any

import requests
import jwt

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean, DateTime
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import IntegrityError


# ============================================================
# CONFIG
# ============================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
CRYPTOCLOUD_SECRET_KEY = os.getenv("CRYPTOCLOUD_SECRET_KEY", "").strip()

TRIAL_LIMIT = int(os.getenv("TRIAL_LIMIT", "5"))

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
POSTBACK_PATH = "/payments/cryptocloud/postback"
CC_CREATE_INVOICE_URL = "https://api.cryptocloud.plus/v1/invoice/create"

PACKAGES = {
    "P30":  {"usd": 3,  "minutes": 30},
    "P60":  {"usd": 8,  "minutes": 60},
    "P180": {"usd": 20, "minutes": 180},
    "P600": {"usd": 50, "minutes": 600},
}


# ============================================================
# DB
# ============================================================

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True)
    target_lang = Column(String, default="en", nullable=False)

    trial_left = Column(Integer, default=TRIAL_LIMIT, nullable=False)
    balance_seconds = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=False)

    order_id = Column(String, nullable=False, unique=True)
    invoice_id = Column(String, nullable=False, unique=True)

    package_code = Column(String, nullable=False)
    amount_usd = Column(Integer, nullable=False)

    status = Column(String, default="created", nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)


# ============================================================
# TELEGRAM HELPERS
# ============================================================

def tg_request(method: str, payload: Dict[str, Any]):
    return requests.post(f"{TG_API}/{method}", json=payload, timeout=30)


def tg_send_message(chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_request("sendMessage", payload)


def format_status(user: User):
    return (
        f"🎙 Голосовой переводчик\n\n"
        f"🌍 Язык: {user.target_lang}\n"
        f"🎁 Бесплатных: {user.trial_left}\n"
        f"💳 Баланс: {user.balance_seconds // 60} мин"
    )


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()


@app.on_event("startup")
def startup():
    init_db()
    log.info("Startup complete")


@app.get("/")
def root():
    return {"ok": True}


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):

    update = await req.json()

    try:
        # ----------------------------------------------------
        # MESSAGE
        # ----------------------------------------------------
        if "message" in update:

            msg = update["message"]
            chat_id = msg["chat"]["id"]

            with SessionLocal() as db:
                user = db.get(User, chat_id)
                if not user:
                    user = User(telegram_id=chat_id)
                    db.add(user)
                    db.commit()
                    db.refresh(user)

                # ---------------- START ----------------
                if msg.get("text") == "/start":
                    tg_send_message(chat_id, format_status(user))
                    return JSONResponse({"ok": True})

                # ---------------- VOICE ----------------
                if "voice" in msg:

                    duration = int(msg["voice"].get("duration", 0))
                    log.info(f"VOICE duration: {duration}")

                    # Проверка баланса / trial
                    if user.trial_left > 0:
                        user.trial_left -= 1
                        db.commit()
                        log.info("Used trial")
                    else:
                        if user.balance_seconds < duration:
                            tg_send_message(chat_id, "❌ Недостаточно минут. Купите пакет.")
                            return JSONResponse({"ok": True})

                        user.balance_seconds -= duration
                        db.commit()
                        log.info(f"Debited {duration}s")

                    # Имитация перевода
                    time.sleep(1)

                    tg_send_message(
                        chat_id,
                        f"✅ Перевод выполнен.\nОстаток: {user.balance_seconds // 60} мин"
                    )

                    return JSONResponse({"ok": True})

        return JSONResponse({"ok": True})

    except Exception as e:
        log.exception("telegram_webhook error")
        return JSONResponse({"ok": True})


# ============================================================
# CRYPTOCLOUD POSTBACK
# ============================================================

@app.post(POSTBACK_PATH)
async def cryptocloud_postback(req: Request):

    raw = await req.body()
    payload = json.loads(raw)

    status = (payload.get("status") or "").lower()
    order_id = payload.get("order_id")
    token = payload.get("token")

    try:
        decoded = jwt.decode(token, CRYPTOCLOUD_SECRET_KEY, algorithms=["HS256"])
    except Exception:
        return PlainTextResponse("bad token", status_code=400)

    with SessionLocal() as db:

        pay = db.query(Payment).filter(Payment.order_id == order_id).first()
        if not pay:
            return PlainTextResponse("not found", status_code=200)

        if pay.status == "credited":
            return PlainTextResponse("ok", status_code=200)

        if status not in ("success", "paid"):
            pay.status = status
            db.commit()
            return PlainTextResponse("ok", status_code=200)

        pkg = PACKAGES.get(pay.package_code.strip().upper())
        if not pkg:
            return PlainTextResponse("ok", status_code=200)

        add_seconds = pkg["minutes"] * 60

        user = db.get(User, pay.telegram_id)
        if not user:
            user = User(telegram_id=pay.telegram_id)
            db.add(user)
            db.commit()
            db.refresh(user)

        before = user.balance_seconds
        user.balance_seconds += add_seconds
        pay.status = "credited"
        db.commit()

        tg_send_message(
            user.telegram_id,
            f"✅ Оплата получена!\nНачислено {pkg['minutes']} мин\nБаланс: {user.balance_seconds // 60} мин"
        )

    return PlainTextResponse("ok", status_code=200)
