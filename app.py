import os
import time
import json
import tempfile
import subprocess
from typing import Optional, Dict, Any

import requests
import jwt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from openai import OpenAI

from db import SessionLocal, engine
from models import User, Payment
from db import Base

# --- DB init ---
Base.metadata.create_all(bind=engine)

# --- ENV ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
CRYPTOCLOUD_SECRET_KEY = os.getenv("CRYPTOCLOUD_SECRET_KEY", "").strip()

BASE_URL = os.getenv("BASE_URL", "").strip()  # https://tg-voice-translator.onrender.com
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")  # опционально для /stats

# Models (можно менять env-ами)
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")  # или tts-1

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN env var is required")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY env var is required")

client = OpenAI(api_key=OPENAI_API_KEY)

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# --- Packages: фиксированные ---
PACKAGES = {
    "P30": {"minutes": 30, "usd": 3},
    "P60": {"minutes": 60, "usd": 8},
    "P180": {"minutes": 180, "usd": 20},
    "P600": {"minutes": 600, "usd": 50},
}

TRIAL_MAX_SECONDS = 60  # 1 минута на бесплатное сообщение

LANGS = {
    "en": "English",
    "ru": "Русский",
    "de": "Deutsch",
    "es": "Español",
    "th": "ไทย",
    "vi": "Tiếng Việt",
    "fr": "Français",
    "tr": "Türkçe",
}

app = FastAPI()


# ---------------- Telegram helpers ----------------
def tg_send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    r = requests.post(f"{TG_API}/sendMessage", data=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def tg_answer_callback(callback_query_id: str):
    r = requests.post(f"{TG_API}/answerCallbackQuery", data={"callback_query_id": callback_query_id}, timeout=30)
    r.raise_for_status()
    return r.json()


def tg_get_file(file_id: str) -> dict:
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    return r.json()


def tg_download_file(file_path: str) -> bytes:
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def tg_send_voice(chat_id: int, voice_bytes: bytes, caption: Optional[str] = None):
    files = {"voice": ("voice.mp3", voice_bytes, "audio/mpeg")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    r = requests.post(f"{TG_API}/sendVoice", data=data, files=files, timeout=120)
    r.raise_for_status()
    return r.json()


def tg_inline_keyboard_langs(selected: str):
    rows = []
    row = []
    for code, name in LANGS.items():
        label = f"✅ {name}" if code == selected else name
        row.append({"text": label, "callback_data": f"lang:{code}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "💳 Купить минуты", "callback_data": "buy"}])
    return {"inline_keyboard": rows}


def tg_inline_keyboard_packages():
    rows = []
    for code in ["P30", "P60", "P180", "P600"]:
        p = PACKAGES[code]
        rows.append([{"text": f"{p['minutes']} мин — ${p['usd']}", "callback_data": f"pack:{code}"}])
    rows.append([{"text": "⬅️ Назад", "callback_data": "back"}])
    return {"inline_keyboard": rows}


# ---------------- DB helpers ----------------
def get_or_create_user(telegram_id: int) -> User:
    db = SessionLocal()
    try:
        user = db.get(User, telegram_id)
        if not user:
            user = User(telegram_id=telegram_id, target_lang="en", trial_messages=5, balance_seconds=0)
            db.add(user)
            db.commit()
            db.refresh(user)
        return user
    finally:
        db.close()


def update_user_lang(telegram_id: int, lang: str):
    db = SessionLocal()
    try:
        user = db.get(User, telegram_id)
        if not user:
            user = User(telegram_id=telegram_id, target_lang=lang, trial_messages=5, balance_seconds=0)
            db.add(user)
        else:
            user.target_lang = lang
        db.commit()
    finally:
        db.close()


def debit_for_voice(telegram_id: int, duration_sec: int) -> Dict[str, Any]:
    """
    Возвращает:
      {"allowed": bool, "reason": str, "trial_used": bool, "balance_left": int, "trial_left": int}
    """
    db = SessionLocal()
    try:
        user = db.get(User, telegram_id)
        if not user:
            user = User(telegram_id=telegram_id, target_lang="en", trial_messages=5, balance_seconds=0)
            db.add(user)
            db.commit()
            db.refresh(user)

        # Trial
        if user.trial_messages > 0:
            if duration_sec > TRIAL_MAX_SECONDS:
                return {
                    "allowed": False,
                    "reason": f"Бесплатно можно до {TRIAL_MAX_SECONDS} секунд. Сократи голосовое или купи минуты.",
                    "trial_used": False,
                    "balance_left": user.balance_seconds,
                    "trial_left": user.trial_messages,
                }
            user.trial_messages -= 1
            db.commit()
            return {
                "allowed": True,
                "reason": "trial",
                "trial_used": True,
                "balance_left": user.balance_seconds,
                "trial_left": user.trial_messages,
            }

        # Paid balance
        if user.balance_seconds >= duration_sec:
            user.balance_seconds -= duration_sec
            db.commit()
            return {
                "allowed": True,
                "reason": "paid",
                "trial_used": False,
                "balance_left": user.balance_seconds,
                "trial_left": user.trial_messages,
            }

        return {
            "allowed": False,
            "reason": "Баланс минут закончился. Нажми «Купить минуты».",
            "trial_used": False,
            "balance_left": user.balance_seconds,
            "trial_left": user.trial_messages,
        }
    finally:
        db.close()


def credit_minutes(telegram_id: int, minutes: int):
    db = SessionLocal()
    try:
        user = db.get(User, telegram_id)
        if not user:
            user = User(telegram_id=telegram_id, target_lang="en", trial_messages=5, balance_seconds=0)
            db.add(user)
            db.commit()
            db.refresh(user)
        user.balance_seconds += minutes * 60
        db.commit()
        return user.balance_seconds
    finally:
        db.close()


def get_user_status_text(telegram_id: int) -> str:
    db = SessionLocal()
    try:
        user = db.get(User, telegram_id)
        if not user:
            user = User(telegram_id=telegram_id, target_lang="en", trial_messages=5, balance_seconds=0)
            db.add(user)
            db.commit()
            db.refresh(user)

        bal_min = user.balance_seconds // 60
        return (
            f"🎙 Голосовой переводчик\n\n"
            f"🌍 Язык перевода: {LANGS.get(user.target_lang, user.target_lang)}\n"
            f"🎁 Бесплатных переводов: {user.trial_messages} (≤ {TRIAL_MAX_SECONDS} сек)\n"
            f"💳 Баланс: {bal_min} мин\n\n"
            f"Запиши голосовое — я переведу и пришлю озвучку."
        )
    finally:
        db.close()


# ---------------- OpenAI pipeline ----------------
def ffmpeg_to_mp3(input_bytes: bytes, input_ext: str) -> bytes:
    """
    Конвертируем OGG/OPUS (Telegram voice) -> MP3 для транскрибации.
    """
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, f"input.{input_ext}")
        out_path = os.path.join(td, "output.mp3")
        with open(in_path, "wb") as f:
            f.write(input_bytes)

        cmd = ["ffmpeg", "-y", "-i", in_path, "-vn", "-acodec", "libmp3lame", "-ar", "16000", "-ac", "1", out_path]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode('utf-8', 'ignore')[:500]}")

        with open(out_path, "rb") as f:
            return f.read()


def openai_transcribe(mp3_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=True) as tmp:
        tmp.write(mp3_bytes)
        tmp.flush()
        with open(tmp.name, "rb") as f:
            tr = client.audio.transcriptions.create(
                model=TRANSCRIBE_MODEL,
                file=f,
            )
    # SDK возвращает объект с .text
    return getattr(tr, "text", "") or ""


def openai_translate(text: str, target_lang: str) -> str:
    lang_name = LANGS.get(target_lang, target_lang)
    resp = client.responses.create(
        model=CHAT_MODEL,
        input=[
            {
                "role": "system",
                "content": "You are a professional translator. Preserve meaning, names, numbers, and medical terms. Output only the translation.",
            },
            {
                "role": "user",
                "content": f"Translate the following text into {lang_name}:\n\n{text}",
            },
        ],
    )
    # responses API: берем текст из output_text
    out = getattr(resp, "output_text", None)
    if out:
        return out.strip()
    # fallback (на всякий)
    try:
        return resp.output[0].content[0].text.strip()
    except Exception:
        return ""


def openai_tts(text: str) -> bytes:
    speech = client.audio.speech.create(
        model=TTS_MODEL,
        voice="alloy",
        input=text,
        format="mp3",
    )
    # speech возвращает bytes через .read()
    return speech.read()


# ---------------- CryptoCloud ----------------
def cryptocloud_create_invoice(telegram_id: int, package_code: str) -> str:
    """
    Возвращает URL на оплату (страница инвойса).
    """
    if not (CRYPTOCLOUD_API_KEY and CRYPTOCLOUD_SHOP_ID):
        raise RuntimeError("CryptoCloud env vars missing: CRYPTOCLOUD_API_KEY / CRYPTOCLOUD_SHOP_ID")

    if package_code not in PACKAGES:
        raise ValueError("Unknown package")

    p = PACKAGES[package_code]
    amount = p["usd"]

    # order_id кодируем: tgId_package_timestamp
    order_id = f"{telegram_id}_{package_code}_{int(time.time())}"

    url = "https://api.cryptocloud.plus/v2/invoice/create"
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}"}
    payload = {
        "shop_id": CRYPTOCLOUD_SHOP_ID,
        "amount": amount,
        "currency": "USD",
        "order_id": order_id,
    }

    # эти поля не всегда обязательны, но полезны
    if BASE_URL:
        payload["success_url"] = f"{BASE_URL}/payment-success"
        payload["fail_url"] = f"{BASE_URL}/payment-failed"

    r = requests.post(url, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()

    # ожидаем { "status": "success", "result": { "link": "...", "uuid": "..."} }
    if not data or data.get("status") != "success":
        raise RuntimeError(f"CryptoCloud invoice create failed: {data}")

    link = data.get("result", {}).get("link")
    invoice_id = data.get("result", {}).get("uuid") or data.get("result", {}).get("invoice_id", "")

    # сохраняем платеж
    db = SessionLocal()
    try:
        pay = Payment(
            telegram_id=telegram_id,
            order_id=order_id,
            invoice_id=str(invoice_id or ""),
            package_code=package_code,
            amount_usd=amount,
            status="created",
        )
        db.add(pay)
        db.commit()
    finally:
        db.close()

    if not link:
        raise RuntimeError(f"CryptoCloud did not return payment link: {data}")

    return link


def cryptocloud_verify_token(token: str) -> dict:
    if not CRYPTOCLOUD_SECRET_KEY:
        raise RuntimeError("CRYPTOCLOUD_SECRET_KEY is required to verify postback token")
    try:
        decoded = jwt.decode(token, CRYPTOCLOUD_SECRET_KEY, algorithms=["HS256"])
        return decoded
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid postback token: {e}")


def _is_paid_status(status: str) -> bool:
    s = (status or "").lower()
    return s in {"paid", "success", "completed", "confirmed", "done"}


# ---------------- Routes ----------------
@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/payment-success", response_class=HTMLResponse)
def payment_success():
    return "<h2>Payment success</h2><p>You can return to Telegram.</p>"


@app.get("/payment-failed", response_class=HTMLResponse)
def payment_failed():
    return "<h2>Payment failed</h2><p>Please try again or return to Telegram.</p>"


@app.post("/payments/cryptocloud/postback")
async def cryptocloud_postback(request: Request):
    """
    CryptoCloud присылает postback. Часто в payload есть поле "token" (JWT).
    Мы проверяем token, затем начисляем минуты.
    """
    payload = await request.json()

    token = payload.get("token")
    if token:
        data = cryptocloud_verify_token(token)
    else:
        # Если вдруг прислали без токена (нежелательно) — используем как есть
        data = payload

    status = str(data.get("status") or data.get("invoice_status") or "")
    order_id = str(data.get("order_id") or "")
    invoice_id = str(data.get("invoice_id") or data.get("uuid") or "")

    if not order_id:
        return JSONResponse({"ok": True, "ignored": True})

    # order_id = tgId_package_timestamp
    try:
        parts = order_id.split("_")
        telegram_id = int(parts[0])
        package_code = parts[1]
    except Exception:
        return JSONResponse({"ok": True, "ignored": True})

    # обновим платеж в БД
    db = SessionLocal()
    try:
        pay = db.query(Payment).filter(Payment.order_id == order_id).first()
        if pay:
            pay.status = status or pay.status
            if invoice_id:
                pay.invoice_id = invoice_id
            db.commit()
    finally:
        db.close()

    if _is_paid_status(status):
        minutes = PACKAGES.get(package_code, {}).get("minutes")
        if minutes:
            new_balance = credit_minutes(telegram_id, minutes)
            tg_send_message(
                telegram_id,
                f"✅ Оплата подтверждена.\nНачислено: +{minutes} мин.\nБаланс: {new_balance // 60} мин."
            )

    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()

    # Callback query (кнопки)
    if "callback_query" in update:
        cq = update["callback_query"]
        cq_id = cq.get("id")
        data = cq.get("data", "")
        from_user = cq.get("from", {})
        telegram_id = int(from_user.get("id"))

        if cq_id:
            tg_answer_callback(cq_id)

        if data.startswith("lang:"):
            lang = data.split(":", 1)[1]
            if lang in LANGS:
                update_user_lang(telegram_id, lang)
            user = get_or_create_user(telegram_id)
            tg_send_message(telegram_id, get_user_status_text(telegram_id), reply_markup=tg_inline_keyboard_langs(user.target_lang))
            return {"ok": True}

        if data == "buy":
            tg_send_message(
                telegram_id,
                "💳 Выбери пакет минут:",
                reply_markup=tg_inline_keyboard_packages(),
            )
            return {"ok": True}

        if data == "back":
            user = get_or_create_user(telegram_id)
            tg_send_message(telegram_id, get_user_status_text(telegram_id), reply_markup=tg_inline_keyboard_langs(user.target_lang))
            return {"ok": True}

        if data.startswith("pack:"):
            package_code = data.split(":", 1)[1]
            try:
                link = cryptocloud_create_invoice(telegram_id, package_code)
                p = PACKAGES[package_code]
                tg_send_message(
                    telegram_id,
                    f"🧾 Счёт создан: {p['minutes']} мин за ${p['usd']}\n\n"
                    f"Оплати по ссылке:\n{link}\n\n"
                    f"После оплаты минуты начислятся автоматически ✅"
                )
            except Exception as e:
                tg_send_message(telegram_id, f"Ошибка создания счёта: {e}")
            return {"ok": True}

        return {"ok": True}

    # Message
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}

    chat = msg.get("chat", {})
    chat_id = int(chat.get("id"))
    from_user = msg.get("from", {})
    telegram_id = int(from_user.get("id", chat_id))

    text = (msg.get("text") or "").strip()

    # Ensure user exists
    user = get_or_create_user(telegram_id)

    # Commands
    if text == "/start":
        tg_send_message(chat_id, get_user_status_text(telegram_id), reply_markup=tg_inline_keyboard_langs(user.target_lang))
        return {"ok": True}

    if text in ("/buy", "buy"):
        tg_send_message(chat_id, "💳 Выбери пакет минут:", reply_markup=tg_inline_keyboard_packages())
        return {"ok": True}

    if text == "/stats" and ADMIN_ID and telegram_id == ADMIN_ID:
        db = SessionLocal()
        try:
            users_count = db.query(User).count()
            paid_sum = db.query(User).with_entities(User.balance_seconds).all()
            trial_sum = db.query(User).with_entities(User.trial_messages).all()
            paid_minutes = sum((x[0] or 0) for x in paid_sum) // 60
            trial_left = sum((x[0] or 0) for x in trial_sum)
        finally:
            db.close()
        tg_send_message(chat_id, f"📊 Stats\n\n👥 Users: {users_count}\n💳 Paid minutes total: {paid_minutes}\n🎁 Trial messages left total: {trial_left}")
        return {"ok": True}

    # Voice
    if "voice" in msg:
        voice = msg["voice"]
        file_id = voice.get("file_id")
        duration = int(voice.get("duration") or 0)

        check = debit_for_voice(telegram_id, duration)
        if not check["allowed"]:
            tg_send_message(chat_id, check["reason"], reply_markup=tg_inline_keyboard_packages())
            return {"ok": True}

        # download ogg/opus
        info = tg_get_file(file_id)
        file_path = info.get("result", {}).get("file_path")
        if not file_path:
            tg_send_message(chat_id, "Не смог скачать файл (file_path пустой). Попробуй ещё раз.")
            return {"ok": True}

        ogg_bytes = tg_download_file(file_path)

        try:
            mp3_bytes = ffmpeg_to_mp3(ogg_bytes, "ogg")
            transcript = openai_transcribe(mp3_bytes).strip()
            if not transcript:
                tg_send_message(chat_id, "Не удалось распознать речь. Попробуй говорить чуть медленнее/громче.")
                return {"ok": True}

            translated = openai_translate(transcript, user.target_lang).strip()
            if not translated:
                tg_send_message(chat_id, "Не удалось перевести текст. Попробуй ещё раз.")
                return {"ok": True}

            audio = openai_tts(translated)
            caption = f"📝 {translated}"
            tg_send_voice(chat_id, audio, caption=caption)

            # покажем остатки
            user2 = get_or_create_user(telegram_id)
            bal_min = user2.balance_seconds // 60
            tg_send_message(
                chat_id,
                f"✅ Готово.\n🎁 Free: {user2.trial_messages} (≤ {TRIAL_MAX_SECONDS} сек)\n💳 Баланс: {bal_min} мин",
                reply_markup=tg_inline_keyboard_langs(user2.target_lang),
            )

        except Exception as e:
            tg_send_message(chat_id, f"Ошибка обработки аудио: {e}")

        return {"ok": True}

    # Plain text fallback
    if text:
        tg_send_message(chat_id, "Отправь голосовое или выбери язык кнопками 👇", reply_markup=tg_inline_keyboard_langs(user.target_lang))

    return {"ok": True}
