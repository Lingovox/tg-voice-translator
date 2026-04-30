import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ============================
# Configuration
# ============================
# Изменено на TELEGRAM_BOT_TOKEN по вашему запросу
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "lingovox_bot")
TRIAL_LIMIT = 5
TRIAL_MAX_SECONDS = 60

# Проверка наличия токена перед запуском
if not TOKEN:
    logging.error("ERROR: TELEGRAM_BOT_TOKEN is not set in environment variables!")

app = FastAPI()
bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher(bot) if bot else None

# ============================
# Constants: Языковая панель
# ============================
LANGS = [
    ("English", "en"),
    ("Русский", "ru"),
    ("O'zbek", "uz"),
    ("हिन्दी", "hi"),
    ("Español", "es"),
    ("中文", "zh"),
    ("العربية", "ar"),
    ("Português", "pt"),
    ("Türkçe", "tr"),
    ("Қазақша", "kk"),
]

LANG_LABELS = {code: name for name, code in LANGS}

# Алиасы для режима Conversation и голосовых команд[cite: 1]
LANG_ALIASES = {
    "en": ["english", "английский", "ingliz", "английском"],
    "ru": ["russian", "русский", "русском", "русскую"],
    "uz": ["uzbek", "узбекский", "o'zbek", "узбекча", "узбекском"],
    "hi": ["hindi", "хинди", "hindi language"],
    "es": ["spanish", "испанский", "español", "испанском"],
    "zh": ["chinese", "китайский", "中文", "китайском"],
    "ar": ["arabic", "арабский", "العربية", "арабском"],
    "pt": ["portuguese", "португальский", "português", "португальском"],
    "tr": ["turkish", "турецкий", "türkçe", "турецком"],
    "kk": ["kazakh", "казахский", "қазақша", "казакша", "казахском"],
}

# ============================
# Landing Page: FastAPI Route[cite: 1]
# ============================
@app.get("/", response_class=HTMLResponse)
def landing():
    bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "#"
    langs_display = ", ".join([name for name, _ in LANGS])

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lingovox — AI Voice Translator</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; background: #f4f7f9; color: #222; margin: 0; padding: 20px; }}
    .wrap {{ max-width: 900px; margin: 0 auto; }}
    .card {{ background: #fff; border: 1px solid #e1e8ed; padding: 24px; border-radius: 12px; margin-top: 20px; shadow: 0 2px 4px rgba(0,0,0,0.05); }}
    .btn {{ display: inline-block; background: #0088cc; color: #fff; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: 600; }}
    .price {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #f0f0f0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Lingovox: Translate Voice Instantly</h1>
      <p>AI-powered voice-to-voice translation in Telegram. Support for Kazakh, Uzbek and more.[cite: 1]</p>
      <a class="btn" href="{bot_link}">Start Translating ↗</a>
      
      <h2>Supported Languages</h2>
      <p>{langs_display}</p>
    </div>
    
    <div class="card">
      <h2>Pricing & Payments</h2>
      <div class="price"><b>30 min</b><span>$10</span></div>
      <div class="price"><b>60 min</b><span>$15</span></div>
      <p style="margin-top:15px; font-size: 0.9em; color: #666;">
        Secure payments via <b>Paddle</b> and <b>NOWPayments</b>.[cite: 1]
      </p>
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)

# ============================
# Bot Handlers
# ============================
if dp:
    @dp.message_handler(commands=['start'])
    async def send_welcome(message: types.Message):
        keyboard = InlineKeyboardMarkup(row_width=2)
        buttons = [InlineKeyboardButton(text=name, callback_data=f"set_lang_{code}") for name, code in LANGS]
        keyboard.add(*buttons)
        await message.answer("Select target language:", reply_markup=keyboard)

    @dp.callback_query_handler(lambda c: c.data.startswith('set_lang_'))
    async def process_lang(callback_query: types.CallbackQuery):
        lang_code = callback_query.data.split('_')[-1]
        lang_name = LANG_LABELS.get(lang_code, lang_code)
        await bot.answer_callback_query(callback_query.id)
        await bot.send_message(callback_query.from_user.id, f"Target language: {lang_name}")

if __name__ == '__main__':
    from aiogram import executor
    if dp:
        executor.start_polling(dp, skip_updates=True)
