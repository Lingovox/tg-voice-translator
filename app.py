import os
import time
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any

import requests
import jwt

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean,
    DateTime
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import IntegrityError


# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

# ----------------------------
# Env
# ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
CRYPTOCLOUD_SECRET_KEY = os.getenv("CRYPTOCLOUD_SECRET_KEY", "").strip()

TRIAL_LIMIT = int(os.getenv("TRIAL_LIMIT", "5"))

# ----------------------------
# Constants
# ----------------------------
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# CryptoCloud v1 endpoints (ВАЖНО: без /api/v1)
CC_CREATE_INVOICE_URL = "https://api.cryptocloud.plus/v1/invoice/create"
CC_INVOICE_INFO_URL = "https://api.cryptocloud.plus/v1/invoice/info"

POSTBACK_PATH = "/payments/cryptocloud/postback"

# Package mapping: minutes -> price USD
PACKAGES = {
    "P30":  {"usd": 3,  "minutes": 30},
    "P60":  {"usd": 8,  "minutes": 60},
    "P180": {"usd": 20, "minutes": 180},
    "P600": {"usd": 50, "minutes": 600},
}

LANGS = [
    ("English",  "en"),
    ("Русский",  "ru"),
    ("Deutsch",  "de"),
    ("Español",  "es"),
    ("ไทย",      "th"),
    ("Tiếng Việt","vi"),
    ("Français", "fr"),
    ("Türkçe",   "tr"),
]

# ----------------------------
# DB
# ----------------------------
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True, index=True, nullable=False)
    target_lang = Column(String, nullable=False, default="en")

    # trial_left — сколько бесплатных сообщений осталось
    trial_left = Column(Integer, nullable=False, default=TRIAL_LIMIT)

    # trial_messages — можно хранить "сколько уже использовал" (если у тебя так задумано)
    trial_messages = Column(Integer, nullable=False, default=0)

    # баланс в секундах
    balance_seconds = Column(Integer, nullable=False, default=0)

    is_subscribed = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)

    order_id = Column(String, nullable=False, unique=True, index=True)

    # ВАЖНО: invoice_id NOT NULL (как в твоей БД)
    invoice_id = Column(String, nullable=False, unique=True, index=True)

    package_code = Column(String, nullable=False)
    amount_usd = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="created")  # created/paid/success/failed...

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)


# ----------------------------
# Telegram helpers
# ----------------------------
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


def build_main_keyboard(selected_lang: str) -> Dict[str, Any]:
    # 2 колонки, 4 строки, + последняя большая кнопка "Купить минуты"
    rows = []
    # по 2 кнопки в строке
    for i in range(0, len(LANGS), 2):
        pair = LANGS[i:i+2]
        row = []
        for title, code in pair:
            prefix = "✅ " if code == selected_lang else ""
            row.append({"text": f"{prefix}{title}", "callback_data": f"lang:{code}"})
        rows.append(row)

    rows.append([{"text": "💳 Купить минуты", "callback_data": "buy:menu"}])

    return {"inline_keyboard": rows}


def build_packages_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "30 мин — $3", "callback_data": "buy:P30"}],
            [{"text": "60 мин — $8", "callback_data": "buy:P60"}],
            [{"text": "180 мин — $20", "callback_data": "buy:P180"}],
            [{"text": "600 мин — $50", "callback_data": "buy:P600"}],
            [{"text": "⬅️ Назад", "callback_data": "buy:back"}],
        ]
    }


def format_status_text(user: User) -> str:
    bal_min = user.balance_seconds // 60
    return (
        "🎙 Голосовой переводчик\n\n"
        f"🌍 Язык перевода: {user.target_lang}\n"
        f"🎁 Бесплатных переводов: {user.trial_left} (≤ 60 сек)\n"
        f"💳 Баланс: {bal_min} мин\n\n"
        "Запиши голосовое — я переведу и пришлю озвучку."
    )


# ----------------------------
# CryptoCloud helpers
# ----------------------------
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
    """
    Создает инвойс в CryptoCloud.
    ВАЖНО: URL должен быть https://api.cryptocloud.plus/v1/invoice/create (без /api/v1)
    """
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

    # если Cloudflare/HTML — покажем raw
    ct = (r.headers.get("content-type") or "").lower()
    if "application/json" not in ct:
        return {"ok": False, "status": r.status_code, "raw": r.text}

    data = r.json()
    return {"ok": r.status_code == 200, "status": r.status_code, "data": data}


def verify_postback_token(token: str) -> Optional[dict]:
    """
    В postback приходит JWT token. Проверяем подпись HS256 через CRYPTOCLOUD_SECRET_KEY.
    """
    try:
        decoded = jwt.decode(token, CRYPTOCLOUD_SECRET_KEY, algorithms=["HS256"])
        return decoded
    except Exception as e:
        log.warning(f"JWT verify failed: {e}")
        return None


# ----------------------------
# FastAPI
# ----------------------------
app = FastAPI()


@app.on_event("startup")
def startup():
    init_db()
    log.info("Startup complete")


@app.get("/")
def root():
    return {"ok": True, "service": "tg-voice-translator"}


@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    """
    Обрабатывает:
    - /start
    - callback кнопок (языки, покупка)
    """
    update = await req.json()
    # log.info(f"TG update: {update}")

    try:
        if "message" in update:
            msg = update["message"]
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            text = msg.get("text", "")

            if not chat_id:
                return JSONResponse({"ok": True})

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

            if text == "/buy":
                # если человек руками ввел
                with SessionLocal() as db:
                    user = db.get(User, int(chat_id))
                    if not user:
                        user = User(telegram_id=int(chat_id), target_lang="en", trial_left=TRIAL_LIMIT)
                        db.add(user)
                        db.commit()
                        db.refresh(user)
                tg_send_message(chat_id, "💳 Выбери пакет минут:", reply_markup=build_packages_keyboard())
                return JSONResponse({"ok": True})

            # (здесь можно обработать voice и перевод — оставляем твою логику отдельно)
            return JSONResponse({"ok": True})

        if "callback_query" in update:
            cq = update["callback_query"]
            data = cq.get("data", "")
            message = cq.get("message", {})
            chat_id = message.get("chat", {}).get("id")

            if not chat_id:
                return JSONResponse({"ok": True})

            # Язык
            if data.startswith("lang:"):
                lang = data.split(":", 1)[1]
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

                tg_request("answerCallbackQuery", {"callback_query_id": cq["id"]})
                return JSONResponse({"ok": True})

            # Покупка
            if data == "buy:menu":
                tg_send_message(chat_id, "💳 Выбери пакет минут:", reply_markup=build_packages_keyboard())
                tg_request("answerCallbackQuery", {"callback_query_id": cq["id"]})
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
                tg_request("answerCallbackQuery", {"callback_query_id": cq["id"]})
                return JSONResponse({"ok": True})

            if data.startswith("buy:"):
                package_code = data.split(":", 1)[1]
                if package_code not in PACKAGES:
                    tg_send_message(chat_id, "Неизвестный пакет.")
                    tg_request("answerCallbackQuery", {"callback_query_id": cq["id"]})
                    return JSONResponse({"ok": True})

                missing = env_missing()
                if missing:
                    tg_send_message(chat_id, f"Ошибка: env vars missing: {', '.join(missing)}")
                    tg_request("answerCallbackQuery", {"callback_query_id": cq["id"]})
                    return JSONResponse({"ok": True})

                amount_usd = PACKAGES[package_code]["usd"]
                order_id = f"{chat_id}_{package_code}_{int(time.time())}"
                description = f"Minutes package {package_code} for user {chat_id}"

                # Create invoice in CryptoCloud
                cc = cryptocloud_create_invoice(order_id=order_id, amount_usd=amount_usd, description=description)
                if not cc["ok"]:
                    tg_send_message(
                        chat_id,
                        f"Ошибка создания счёта: CryptoCloud create invoice failed: {CC_CREATE_INVOICE_URL} -> "
                        f"HTTP {cc.get('status')}: {cc.get('raw') or cc.get('data')}"
                    )
                    tg_request("answerCallbackQuery", {"callback_query_id": cq["id"]})
                    return JSONResponse({"ok": True})

                data_json = cc["data"]

                # В ответах встречаются разные структуры. Достаём максимально безопасно:
                # Чаще всего: {"status":"success","result":{"uuid":"INV-XXXX","link":"https://pay..."}}
                result = data_json.get("result") or data_json.get("data") or data_json
                invoice_uuid = result.get("uuid") or result.get("invoice_id") or result.get("id")
                pay_url = result.get("link") or result.get("pay_url") or result.get("url")

                if not invoice_uuid:
                    tg_send_message(chat_id, f"CryptoCloud ответ без invoice uuid: {data_json}")
                    tg_request("answerCallbackQuery", {"callback_query_id": cq["id"]})
                    return JSONResponse({"ok": True})

                # Save payment in DB (invoice_id NOT NULL!)
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
                        # если order_id или invoice_id уже есть — покажем ссылку повторно
                    except Exception as e:
                        db.rollback()
                        tg_send_message(chat_id, f"DB error: {e}")
                        tg_request("answerCallbackQuery", {"callback_query_id": cq["id"]})
                        return JSONResponse({"ok": True})

                if pay_url:
                    kb = {
                        "inline_keyboard": [
                            [{"text": "Перейти к оплате ✅", "url": pay_url}],
                            [{"text": "Проверить оплату 🔄", "callback_data": f"check:{invoice_uuid}"}],
                        ]
                    }
                    tg_send_message(chat_id, f"Счёт создан. Сумма: ${amount_usd}\nПакет: {package_code}", reply_markup=kb)
                else:
                    tg_send_message(chat_id, f"Счёт создан: {invoice_uuid}\n(В ответе не было ссылки оплаты)")

                tg_request("answerCallbackQuery", {"callback_query_id": cq["id"]})
                return JSONResponse({"ok": True})

            # Проверка статуса (опционально)
            if data.startswith("check:"):
                invoice_id = data.split(":", 1)[1]
                tg_send_message(chat_id, f"Статус счёта: {invoice_id}\n(Проверка сейчас через postback/вручную)")
                tg_request("answerCallbackQuery", {"callback_query_id": cq["id"]})
                return JSONResponse({"ok": True})

            tg_request("answerCallbackQuery", {"callback_query_id": cq["id"]})
            return JSONResponse({"ok": True})

        return JSONResponse({"ok": True})
    except Exception as e:
        log.exception("telegram_webhook error")
        return JSONResponse({"ok": True, "error": str(e)})


@app.post(POSTBACK_PATH)
async def cryptocloud_postback(req: Request):
    """
    Сюда CryptoCloud шлёт уведомления.
    Мы:
    - проверяем JWT token подписью secret_key
    - находим payment по order_id / invoice_id
    - если paid/success — начисляем секунды
    """
    raw = await req.body()
    try:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = json.loads(raw)

        log.info("==== RAW POSTBACK ====")
        log.info(payload)

        status = (payload.get("status") or "").lower()
        order_id = payload.get("order_id")
        token = payload.get("token")

        if not token:
            return PlainTextResponse("no token", status_code=400)

        decoded = verify_postback_token(token)
        if not decoded:
            return PlainTextResponse("bad token", status_code=400)

        # invoice id from token
        token_invoice_id = decoded.get("id")

        # В postback бывает:
        # invoice_id: "BOVIBV5N"
        # invoice_info.uuid: "INV-BOVIBV5N"
        postback_invoice_id = payload.get("invoice_id")
        invoice_info = payload.get("invoice_info") or {}
        invoice_uuid = invoice_info.get("uuid")

        # Выберем "главный" invoice_id который точно не пустой:
        effective_invoice_id = invoice_uuid or postback_invoice_id or token_invoice_id
        if not effective_invoice_id and not order_id:
            return PlainTextResponse("no invoice_id/order_id", status_code=400)

        # Статусы "успеха"
        is_paid = status in ("success", "paid")
        invoice_status = (invoice_info.get("invoice_status") or "").lower()
        if invoice_status in ("success", "paid"):
            is_paid = True

        with SessionLocal() as db:
            q = None
            if order_id:
                q = db.query(Payment).filter(Payment.order_id == order_id).first()
            if not q and effective_invoice_id:
                q = db.query(Payment).filter(Payment.invoice_id == str(effective_invoice_id)).first()

            if not q:
                log.warning(f"Payment not found for order_id={order_id} invoice_id={effective_invoice_id}")
                return PlainTextResponse("payment not found", status_code=200)

            # если уже успех — не начисляем повторно
            if (q.status or "").lower() in ("paid", "success"):
                log.info("Already paid, skip")
                return PlainTextResponse("ok", status_code=200)

            if not is_paid:
                # просто обновим статус
                q.status = status or "unknown"
                q.updated_at = datetime.utcnow()
                db.add(q)
                db.commit()
                log.info(f"Status not paid: {q.status}")
                return PlainTextResponse("ok", status_code=200)

            # PAID => начисляем минуты
            pkg = PACKAGES.get(q.package_code)
            if not pkg:
                q.status = "paid"
                q.updated_at = datetime.utcnow()
                db.add(q)
                db.commit()
                return PlainTextResponse("ok", status_code=200)

            add_seconds = int(pkg["minutes"] * 60)

            user = db.get(User, int(q.telegram_id))
            if not user:
                user = User(telegram_id=int(q.telegram_id), target_lang="en", trial_left=TRIAL_LIMIT)
                db.add(user)
                db.commit()
                db.refresh(user)

            user.balance_seconds = int(user.balance_seconds or 0) + add_seconds
            user.updated_at = datetime.utcnow()

            q.status = "paid"
            q.updated_at = datetime.utcnow()

            db.add(user)
            db.add(q)
            db.commit()
            db.refresh(user)

            # уведомим пользователя
            bal_min = user.balance_seconds // 60
            tg_send_message(
                int(user.telegram_id),
                f"✅ Оплата получена!\nПакет: {q.package_code}\nНачислено: {pkg['minutes']} мин\nБаланс: {bal_min} мин",
                reply_markup=build_main_keyboard(user.target_lang),
            )

        return PlainTextResponse("ok", status_code=200)

    except Exception as e:
        log.exception("postback error")
        return PlainTextResponse(f"error: {e}", status_code=200)
