import os
import tempfile
import subprocess
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from openai import OpenAI
from db import SessionLocal, User, init_db

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TRIAL_LIMIT = int(os.environ.get("TRIAL_LIMIT", "5"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()

LANGS = [
    ("en", "🇬🇧 English"),
    ("ru", "🇷🇺 Русский"),
    ("de", "🇩🇪 Deutsch"),
    ("es", "🇪🇸 Español"),
    ("th", "🇹🇭 ไทย"),
    ("vi", "🇻🇳 Tiếng Việt"),
    ("fr", "🇫🇷 Français"),
    ("tr", "🇹🇷 Türkçe"),
]

LANG_ALIASES = {
    "en": ["english", "англий", "ingliz"],
    "ru": ["рус", "russian"],
    "de": ["нем", "german", "deutsch"],
    "es": ["испан", "spanish", "español"],
    "th": ["тай", "thai"],
    "vi": ["вьет", "vietnam", "tiếng việt"],
    "fr": ["франц", "french", "français"],
    "tr": ["турец", "turkish", "türk"],
}

def build_lang_keyboard():
    rows, row = [], []
    for code, label in LANGS:
        row.append({"text": label, "callback_data": f"lang_{code}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}

def detect_target_lang_from_text(text: str):
    t = (text or "").lower()
    for code, aliases in LANG_ALIASES.items():
        for a in aliases:
            if a in t:
                return code
    return None

def tg_send_message(chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TG_API}/sendMessage", json=payload, timeout=30)

def tg_send_voice(chat_id: int, voice_path: str, caption: str | None = None):
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    with open(voice_path, "rb") as f:
        files = {"voice": f}
        requests.post(f"{TG_API}/sendVoice", data=data, files=files, timeout=120)

def tg_answer_callback_query(callback_query_id: str):
    requests.post(f"{TG_API}/answerCallbackQuery", json={"callback_query_id": callback_query_id}, timeout=30)

def tg_get_file_path(file_id: str) -> str:
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    return r.json()["result"]["file_path"]

def tg_download_file(file_path: str) -> bytes:
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content

def get_or_create_user(telegram_id: int) -> User:
    db = SessionLocal()
    try:
        user = db.get(User, telegram_id)
        if not user:
            user = User(telegram_id=telegram_id, trial_left=TRIAL_LIMIT, is_subscribed=False, target_lang="en")
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
            user = User(telegram_id=telegram_id, trial_left=TRIAL_LIMIT, is_subscribed=False, target_lang=lang)
            db.add(user)
        else:
            user.target_lang = lang
        db.commit()
    finally:
        db.close()

def decrement_trial_if_needed(telegram_id: int) -> int:
    db = SessionLocal()
    try:
        user = db.get(User, telegram_id)
        if not user:
            user = User(telegram_id=telegram_id, trial_left=TRIAL_LIMIT, is_subscribed=False, target_lang="en")
            db.add(user)
            db.commit()
            db.refresh(user)

        if user.is_subscribed:
            return user.trial_left

        if user.trial_left > 0:
            user.trial_left -= 1
            db.commit()

        return user.trial_left
    finally:
        db.close()

def stt_transcribe(audio_bytes: bytes) -> str:
    # Save ogg
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp_ogg:
        tmp_ogg.write(audio_bytes)
        ogg_path = tmp_ogg.name

    # Convert to wav 16k mono
    wav_path = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", ogg_path, "-ac", "1", "-ar", "16000", "-vn", wav_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        with open(wav_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=f,
            )
        return result.text
    finally:
        for p in (ogg_path, wav_path):
            try:
                os.remove(p)
            except:
                pass

def translate_text(text: str, target_lang: str) -> str:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a precise translator."},
            {"role": "user", "content": f"Translate to {target_lang}. Return ONLY translation.\n\n{text}"},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

def tts_speak(text: str) -> str:
    out_path = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
    audio = client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="alloy",
        input=text,
    )
    audio.stream_to_file(out_path)
    return out_path

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/")
def root():
    return {"status": "ok"}

@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()

    if "callback_query" in update:
        cq = update["callback_query"]
        cq_id = cq.get("id")
        message = cq.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        from_id = cq.get("from", {}).get("id")
        data = cq.get("data", "")

        if cq_id:
            tg_answer_callback_query(cq_id)

        if data.startswith("lang_"):
            lang = data.replace("lang_", "").strip()
            update_user_lang(from_id, lang)
            tg_send_message(chat_id, f"Язык перевода установлен: {lang}\nПришли голос.", reply_markup=build_lang_keyboard())
        return JSONResponse({"ok": True})

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return JSONResponse({"ok": True})

    chat_id = msg.get("chat", {}).get("id")
    from_id = msg.get("from", {}).get("id")

    text = msg.get("text", "")
    if text and text.startswith("/start"):
        user = get_or_create_user(from_id)
        tg_send_message(
            chat_id,
            "Привет! Я голосовой переводчик.\n"
            "Выбери язык кнопками или скажи текстом: 'Переведи на немецкий'.\n\n"
            f"Пробных переводов осталось: {user.trial_left}",
            reply_markup=build_lang_keyboard(),
        )
        return JSONResponse({"ok": True})

    if text:
        maybe_lang = detect_target_lang_from_text(text)
        if maybe_lang:
            update_user_lang(from_id, maybe_lang)
            tg_send_message(chat_id, f"Ок! Буду переводить на: {maybe_lang}\nПришли голос.", reply_markup=build_lang_keyboard())
        else:
            tg_send_message(chat_id, "Пришли голосовое сообщение или выбери язык кнопками.", reply_markup=build_lang_keyboard())
        return JSONResponse({"ok": True})

    voice = msg.get("voice")
    if voice:
        user = get_or_create_user(from_id)
        if (not user.is_subscribed) and user.trial_left <= 0:
            tg_send_message(chat_id, "Пробные переводы закончились. Нужна подписка (позже подключим OxaPay).",
                            reply_markup=build_lang_keyboard())
            return JSONResponse({"ok": True})

        try:
            file_id = voice["file_id"]
            file_path = tg_get_file_path(file_id)
            audio_bytes = tg_download_file(file_path)

            source_text = stt_transcribe(audio_bytes)

            lang_from_text = detect_target_lang_from_text(source_text)
            if lang_from_text:
                update_user_lang(from_id, lang_from_text)
                user.target_lang = lang_from_text

            translated = translate_text(source_text, user.target_lang)
            voice_mp3 = tts_speak(translated)

            remaining = decrement_trial_if_needed(from_id)

            caption = f"Текст: {source_text}\n\nПеревод ({user.target_lang}): {translated}"
            tg_send_voice(chat_id, voice_mp3, caption=caption)

            if not user.is_subscribed:
                tg_send_message(chat_id, f"Осталось пробных переводов: {remaining}", reply_markup=build_lang_keyboard())
            else:
                tg_send_message(chat_id, "Выбери язык:", reply_markup=build_lang_keyboard())

        except Exception as e:
            tg_send_message(chat_id, f"Ошибка: {e}")
        return JSONResponse({"ok": True})

    tg_send_message(chat_id, "Пришли голос или выбери язык кнопками.", reply_markup=build_lang_keyboard())
    return JSONResponse({"ok": True})

@app.post("/oxapay/webhook")
async def oxapay_webhook(req: Request):
    data = await req.json()
    return JSONResponse({"ok": True, "received": True})
