import os
import time
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

import requests
import jwt

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
    DateTime,
    func,
    text,
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import IntegrityError


# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

# ============================================================
# ENV
# ============================================================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
CRYPTOCLOUD_SECRET_KEY = os.getenv("CRYPTOCLOUD_SECRET_KEY", "").strip()

TRIAL_LIMIT = int(os.getenv("TRIAL_LIMIT", "5"))
TRIAL_MAX_SECONDS = int(os.getenv("TRIAL_MAX_SECONDS", "60"))  # max per trial voice
MIN_BILLABLE_SECONDS = int(os.getenv("MIN_BILLABLE_SECONDS", "1"))

# ============================================================
# CONSTANTS
# ============================================================
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

POSTBACK_PATH = "/payments/cryptocloud/postback"
CC_CREATE_INVOICE_URL = "https://api.cryptocloud.plus/v1/invoice/create"

# Package mapping: minutes -> price USD
PACKAGES = {
    "P30":  {"usd": 3,  "minutes": 30},
    "P60":  {"usd": 8,  "minutes": 60},
    "P180": {"usd": 20, "minutes": 180},
    "P600": {"usd": 50, "minutes": 600},
}

LANGS = [
    ("English",   "en"),
    ("Русский",   "ru"),
    ("Deutsch",   "de"),
    ("Español",   "es"),
    ("ไทย",       "th"),
    ("Tiếng Việt","vi"),
    ("Français",  "fr"),
    ("Türkçe",    "tr"),
    ("中文",       "zh"),
    ("العربية",   "ar"),
]

# ============================================================
# DB
# ============================================================
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True, index=True, nullable=False)
    target_lang = Column(String, nullable=False, default="en")

    trial_left = Column(Integer, nullable=False, default=TRIAL_LIMIT)
    balance_seconds = Column(Integer, nullable=False, default=0)

    is_subscribed = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)

    order_id = Column(String, nullable=False, unique=True, index=True)
    invoice_id = Column(String, nullable=False, unique=True, index=True)  # NOT NULL

    package_code = Column(String, nullable=False)
    amount_usd = Column(Integer, nullable=False)

    # created -> paid -> credited
    status = Column(String, nullable=False, default="created")

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)


# ============================================================
# TELEGRAM HELPERS
# ============================================================
def tg_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{TG_API}/{method}"
    r = requests.post(url, json=payload, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "raw": r.text, "status": r.status_code}


def tg_send_message(chat_id: int, text_: str, reply_markup: Optional[Dict[str, Any]] = None):
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text_}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_request("sendMessage", payload)


def tg_answer_callback(callback_query_id: str):
    tg_request("answerCallbackQuery", {"callback_query_id": callback_query_id})


def build_main_keyboard(selected_lang: str) -> Dict[str, Any]:
    rows = []
    for i in range(0, len(LANGS), 2):
        pair = LANGS[i:i + 2]
        row = []
        for title, code in pair:
            prefix = "✅ " if code == selected_lang else ""
            row.append({"text": f"{prefix}{title}", "callback_data": f"lang:{code}"})
        rows.append(row)
    rows.append([{"text": "💳 Купить минуты", "callback_data": "buy:menu"}])
    rows.append([{"text": "📌 Баланс", "callback_data": "me:balance"}])
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
    bal_min = max(0, int(user.balance_seconds or 0)) // 60
    return (
        "🎙 Голосовой переводчик\n\n"
        f"🌍 Язык перевода: {user.target_lang}\n"
        f"🎁 Бесплатных переводов: {user.trial_left} (≤ {TRIAL_MAX_SECONDS} сек)\n"
        f"💳 Баланс: {bal_min} мин\n\n"
        "Пришли голосовое сообщение — я переведу."
    )


# ============================================================
# CRYPTOCLOUD HELPERS
# ============================================================
def env_missing() -> list:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not BASE_URL:
        missing.append("BASE_URL")
    if not CRYPTOCLOUD_API_KEY:
        missing.append("CRYPTOCLOUD_API_KEY")
    if not CRYPTOCLOUD_SHOP_ID:
        missing.append("CRYPTOCLOUD_SHOP_ID")
    if not CRYPTOCLOUD_SECRET_KEY:
        missing.append("CRYPTOCLOUD_SECRET_KEY")
    return missing


def cryptocloud_create_invoice(order_id: str, amount_usd: int, description: str) -> Dict[str, Any]:
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
    ct = (r.headers.get("content-type") or "").lower()
    if "application/json" not in ct:
        return {"ok": False, "status": r.status_code, "raw": r.text}
    data = r.json()
    return {"ok": r.status_code == 200, "status": r.status_code, "data": data}


def verify_postback_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, CRYPTOCLOUD_SECRET_KEY, algorithms=["HS256"])
    except Exception as e:
        log.warning(f"JWT verify failed: {e}")
        return None


# ============================================================
# BUSINESS LOGIC
# ============================================================
def get_or_create_user(db, chat_id: int) -> User:
    user = db.get(User, int(chat_id))
    if user:
        # safety defaults
        if user.target_lang is None:
            user.target_lang = "en"
        if user.trial_left is None:
            user.trial_left = TRIAL_LIMIT
        if user.balance_seconds is None:
            user.balance_seconds = 0
        return user

    user = User(
        telegram_id=int(chat_id),
        target_lang="en",
        trial_left=TRIAL_LIMIT,
        balance_seconds=0,
        is_subscribed=False,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def seconds_to_bill(duration: int) -> int:
    d = int(duration or 0)
    if d <= 0:
        return 0
    return max(MIN_BILLABLE_SECONDS, d)


def can_use_trial(user: User, duration: int) -> bool:
    return (user.trial_left or 0) > 0 and int(duration) <= TRIAL_MAX_SECONDS


def debit_for_voice(db, user: User, duration: int) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Корректная trial+paid логика, защита от отрицательного баланса.
    Списание делаем ТОЛЬКО если проверка проходит.
    Возвращает:
    - ok
    - message_if_not_ok
    - debit_context (что списали), чтобы можно было логировать
    """
    duration = int(duration or 0)
    bill = seconds_to_bill(duration)

    if bill == 0:
        return False, "Не вижу длительность аудио. Попробуй ещё раз.", {}

    # Trial first
    if can_use_trial(user, duration):
        user.trial_left = max(0, int(user.trial_left or 0) - 1)
        user.updated_at = datetime.utcnow()
        db.add(user)
        db.commit()
        return True, "", {"mode": "trial", "billed_seconds": bill}

    # Paid balance
    bal = int(user.balance_seconds or 0)
    if bal < bill:
        need_min = (bill - bal + 59) // 60
        return False, f"❌ Недостаточно минут. Нужно ещё примерно {need_min} мин.\nНажми «💳 Купить минуты».", {}

    # Debit
    user.balance_seconds = bal - bill
    if user.balance_seconds < 0:
        user.balance_seconds = 0  # hard guard
    user.updated_at = datetime.utcnow()
    db.add(user)
    db.commit()
    return True, "", {"mode": "paid", "billed_seconds": bill}


# ============================================================
# APP
# ============================================================
app = FastAPI()


@app.on_event("startup")
def startup():
    init_db()
    log.info("Startup complete")


@app.get("/")
def root():
    return {"ok": True, "service": "tg-voice-translator"}


# ============================================================
# ADMIN STATS
# ============================================================
def is_admin(chat_id: int) -> bool:
    return bool(ADMIN_ID) and str(chat_id) == str(ADMIN_ID)


def build_stats_text(db) -> str:
    users_total = db.query(func.count(User.telegram_id)).scalar() or 0
    users_with_balance = db.query(func.count(User.telegram_id)).filter(User.balance_seconds > 0).scalar() or 0

    created_cnt = db.query(func.count(Payment.id)).filter(Payment.status == "created").scalar() or 0
    paid_cnt = db.query(func.count(Payment.id)).filter(Payment.status == "paid").scalar() or 0
    credited_cnt = db.query(func.count(Payment.id)).filter(Payment.status == "credited").scalar() or 0

    # revenue по credited (можно и по paid, но credited точнее)
    revenue_usd = db.query(func.coalesce(func.sum(Payment.amount_usd), 0)).filter(Payment.status == "credited").scalar() or 0

    # топ пользователей по балансу
    top = (
        db.query(User.telegram_id, User.balance_seconds)
        .order_by(User.balance_seconds.desc())
        .limit(5)
        .all()
    )
    top_lines = []
    for tg_id, bal_sec in top:
        top_lines.append(f"- {tg_id}: {int(bal_sec or 0)//60} мин")
    top_text = "\n".join(top_lines) if top_lines else "-"

    return (
        "📊 Admin stats\n\n"
        f"👤 Users total: {users_total}\n"
        f"💳 Users with balance: {users_with_balance}\n\n"
        f"🧾 Payments created: {created_cnt}\n"
        f"✅ Payments paid: {paid_cnt}\n"
        f"🏁 Payments credited: {credited_cnt}\n\n"
        f"💰 Revenue (credited): ${revenue_usd}\n\n"
        f"🏆 Top balances:\n{top_text}\n"
    )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================
@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()

    try:
        # ---------------- MESSAGE ----------------
        if "message" in update:
            msg = update["message"]
            chat_id = (msg.get("chat") or {}).get("id")
            text_ = (msg.get("text") or "").strip()

            if not chat_id:
                return JSONResponse({"ok": True})

            with SessionLocal() as db:
                user = get_or_create_user(db, int(chat_id))

                if text_ == "/start":
                    tg_send_message(chat_id, format_status_text(user), reply_markup=build_main_keyboard(user.target_lang))
                    return JSONResponse({"ok": True})

                if text_ == "/buy":
                    tg_send_message(chat_id, "💳 Выбери пакет минут:", reply_markup=build_packages_keyboard())
                    return JSONResponse({"ok": True})

                if text_ == "/me":
                    tg_send_message(chat_id, format_status_text(user), reply_markup=build_main_keyboard(user.target_lang))
                    return JSONResponse({"ok": True})

                if text_ == "/stats":
                    if not is_admin(chat_id):
                        tg_send_message(chat_id, "⛔️ Доступ запрещён.")
                        return JSONResponse({"ok": True})
                    tg_send_message(chat_id, build_stats_text(db))
                    return JSONResponse({"ok": True})

                # ---------------- VOICE ----------------
                if "voice" in msg:
                    duration = int((msg["voice"] or {}).get("duration") or 0)

                    ok, err_text, ctx = debit_for_voice(db, user, duration)
                    if not ok:
                        tg_send_message(chat_id, err_text, reply_markup=build_main_keyboard(user.target_lang))
                        return JSONResponse({"ok": True})

                    # Здесь должна быть твоя реальная логика:
                    # - скачать audio file
                    # - transcribe/translate/tts
                    # Сейчас оставляем заглушку успешной обработки:
                    mode = ctx.get("mode")
                    billed = ctx.get("billed_seconds")
                    remaining_min = max(0, int(user.balance_seconds or 0)) // 60

                    tg_send_message(
                        chat_id,
                        f"✅ Перевод выполнен.\n"
                        f"Списано: {billed} сек ({mode})\n"
                        f"Баланс: {remaining_min} мин\n"
                        f"Trial left: {user.trial_left}",
                        reply_markup=build_main_keyboard(user.target_lang),
                    )
                    return JSONResponse({"ok": True})

                # default: show status
                tg_send_message(chat_id, format_status_text(user), reply_markup=build_main_keyboard(user.target_lang))
                return JSONResponse({"ok": True})

        # ------------- CALLBACK QUERY -------------
        if "callback_query" in update:
            cq = update["callback_query"]
            cb_id = cq.get("id")
            data = (cq.get("data") or "").strip()
            chat_id = (((cq.get("message") or {}).get("chat") or {}).get("id"))

            if cb_id:
                tg_answer_callback(cb_id)

            if not chat_id:
                return JSONResponse({"ok": True})

            with SessionLocal() as db:
                user = get_or_create_user(db, int(chat_id))

                # Language change
                if data.startswith("lang:"):
                    lang = data.split(":", 1)[1].strip()
                    allowed = {c for _, c in LANGS}
                    if lang in allowed:
                        user.target_lang = lang
                        user.updated_at = datetime.utcnow()
                        db.add(user)
                        db.commit()
                        db.refresh(user)

                    tg_send_message(chat_id, format_status_text(user), reply_markup=build_main_keyboard(user.target_lang))
                    return JSONResponse({"ok": True})

                # Balance
                if data == "me:balance":
                    tg_send_message(chat_id, format_status_text(user), reply_markup=build_main_keyboard(user.target_lang))
                    return JSONResponse({"ok": True})

                # Buy menu
                if data == "buy:menu":
                    tg_send_message(chat_id, "💳 Выбери пакет минут:", reply_markup=build_packages_keyboard())
                    return JSONResponse({"ok": True})

                if data == "buy:back":
                    tg_send_message(chat_id, format_status_text(user), reply_markup=build_main_keyboard(user.target_lang))
                    return JSONResponse({"ok": True})

                # Buy package
                if data.startswith("buy:"):
                    package_code = data.split(":", 1)[1].strip().upper()
                    if package_code not in PACKAGES:
                        tg_send_message(chat_id, "Неизвестный пакет.", reply_markup=build_main_keyboard(user.target_lang))
                        return JSONResponse({"ok": True})

                    missing = env_missing()
                    if missing:
                        tg_send_message(chat_id, f"Ошибка: env vars missing: {', '.join(missing)}")
                        return JSONResponse({"ok": True})

                    amount_usd = int(PACKAGES[package_code]["usd"])
                    order_id = f"{chat_id}_{package_code}_{int(time.time())}"
                    description = f"Minutes package {package_code} for user {chat_id}"

                    cc = cryptocloud_create_invoice(order_id, amount_usd, description)
                    if not cc["ok"]:
                        tg_send_message(
                            chat_id,
                            f"Ошибка создания счёта: {CC_CREATE_INVOICE_URL} -> HTTP {cc.get('status')}\n"
                            f"{cc.get('raw') or cc.get('data')}"
                        )
                        return JSONResponse({"ok": True})

                    data_json = cc["data"]
                    result = data_json.get("result") or data_json.get("data") or data_json
                    invoice_uuid = result.get("uuid") or result.get("invoice_id") or result.get("id")
                    pay_url = result.get("link") or result.get("pay_url") or result.get("url")

                    if not invoice_uuid:
                        tg_send_message(chat_id, f"CryptoCloud ответ без invoice uuid: {data_json}")
                        return JSONResponse({"ok": True})

                    # Save payment
                    try:
                        p = Payment(
                            telegram_id=int(chat_id),
                            order_id=order_id,
                            invoice_id=str(invoice_uuid),
                            package_code=package_code,
                            amount_usd=amount_usd,
                            status="created",
                            created_at=datetime.utcnow(),
                            updated_at=datetime.utcnow(),
                        )
                        db.add(p)
                        db.commit()
                    except IntegrityError as e:
                        db.rollback()
                        log.warning(f"Payment insert IntegrityError: {e}")

                    if pay_url:
                        kb = {"inline_keyboard": [[{"text": "Перейти к оплате ✅", "url": pay_url}]]}
                        tg_send_message(
                            chat_id,
                            f"Счёт создан.\nПакет: {package_code}\nСумма: ${amount_usd}\n\n"
                            "Нажми кнопку ниже, чтобы оплатить:",
                            reply_markup=kb
                        )
                    else:
                        tg_send_message(chat_id, f"Счёт создан: {invoice_uuid}\n(В ответе не было ссылки оплаты)")

                    return JSONResponse({"ok": True})

                # fallback
                tg_send_message(chat_id, format_status_text(user), reply_markup=build_main_keyboard(user.target_lang))
                return JSONResponse({"ok": True})

        return JSONResponse({"ok": True})

    except Exception:
        log.exception("telegram_webhook error")
        return JSONResponse({"ok": True})


# ============================================================
# CRYPTOCLOUD POSTBACK
# ============================================================
@app.post(POSTBACK_PATH)
async def cryptocloud_postback(req: Request):
    raw = await req.body()

    try:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = json.loads(raw)

        log.info("==== RAW POSTBACK ====")
        log.info(payload)

        status = (payload.get("status") or "").lower().strip()
        order_id = payload.get("order_id")
        token = payload.get("token")

        if not token:
            return PlainTextResponse("no token", status_code=400)

        decoded = verify_postback_token(token)
        if not decoded:
            return PlainTextResponse("bad token", status_code=400)

        token_invoice_id = decoded.get("id")
        postback_invoice_id = payload.get("invoice_id")
        invoice_info = payload.get("invoice_info") or {}
        invoice_uuid = invoice_info.get("uuid")

        effective_invoice_id = invoice_uuid or postback_invoice_id or token_invoice_id
        if not effective_invoice_id and not order_id:
            return PlainTextResponse("no invoice_id/order_id", status_code=400)

        is_paid = status in ("success", "paid")
        invoice_status = (invoice_info.get("invoice_status") or "").lower().strip()
        if invoice_status in ("success", "paid"):
            is_paid = True

        with SessionLocal() as db:
            pay = None
            if order_id:
                pay = db.query(Payment).filter(Payment.order_id == order_id).first()
            if not pay and effective_invoice_id:
                pay = db.query(Payment).filter(Payment.invoice_id == str(effective_invoice_id)).first()

            if not pay:
                log.warning(f"Payment not found for order_id={order_id} invoice_id={effective_invoice_id}")
                return PlainTextResponse("payment not found", status_code=200)

            current_status = (pay.status or "").lower().strip()

            # idempotency: skip only if already credited
            if current_status == "credited":
                log.info("Already credited, skip")
                return PlainTextResponse("ok", status_code=200)

            if not is_paid:
                pay.status = status or "unknown"
                pay.updated_at = datetime.utcnow()
                db.add(pay)
                db.commit()
                log.info(f"Status not paid: {pay.status}")
                return PlainTextResponse("ok", status_code=200)

            # paid => credit
            pkg_code = (pay.package_code or "").strip().upper()
            pkg = PACKAGES.get(pkg_code)

            # mark paid (visible)
            pay.status = "paid"
            pay.updated_at = datetime.utcnow()
            db.add(pay)
            db.commit()

            if not pkg:
                log.warning(f"Unknown package_code in DB: {pay.package_code}")
                return PlainTextResponse("ok", status_code=200)

            add_seconds = int(pkg["minutes"] * 60)

            user = db.get(User, int(pay.telegram_id))
            if not user:
                user = User(
                    telegram_id=int(pay.telegram_id),
                    target_lang="en",
                    trial_left=TRIAL_LIMIT,
                    balance_seconds=0,
                    is_subscribed=False,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                db.add(user)
                db.commit()
                db.refresh(user)

            before = int(user.balance_seconds or 0)
            user.balance_seconds = before + add_seconds
            user.updated_at = datetime.utcnow()

            pay.status = "credited"
            pay.updated_at = datetime.utcnow()

            db.add(user)
            db.add(pay)
            db.commit()
            db.refresh(user)

            after = int(user.balance_seconds or 0)
            log.info(
                f"CREDITED: tg_id={user.telegram_id} package={pkg_code} +{add_seconds}s "
                f"({before}s -> {after}s) order={pay.order_id}"
            )

            tg_send_message(
                int(user.telegram_id),
                f"✅ Оплата получена!\nПакет: {pkg_code}\nНачислено: {pkg['minutes']} мин\n"
                f"Баланс: {after // 60} мин",
                reply_markup=build_main_keyboard(user.target_lang),
            )

        return PlainTextResponse("ok", status_code=200)

    except Exception:
        log.exception("postback error")
        return PlainTextResponse("error", status_code=200)
