import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Конфигурация (замените на ваши переменные окружения или значения)
TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "lingovox_bot")
TRIAL_LIMIT = 5
TRIAL_MAX_SECONDS = 60

app = FastAPI()
bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

# ============================
# Constants: Обновленный список языков[cite: 1]
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
SUPPORTED_LANG_CODES = {code for _, code in LANGS}

# Алиасы для распознавания языков в голосовых командах[cite: 1]
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
# Landing Page: FastAPI Route
# ============================
@app.get("/", response_class=HTMLResponse)
def landing():
    bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else ""
    langs_list = ", ".join([name for name, _ in LANGS])

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lingovox — AI Voice Translator</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f4f7f9; color: #222; margin: 0; padding: 20px; line-height: 1.5; }}
    .wrap {{ max-width: 900px; margin: 0 auto; }}
    .badge {{ display: inline-flex; align-items: center; background: #fff; border: 1px solid #e1e8ed; padding: 4px 12px; border-radius: 20px; font-size: 14px; font-weight: 600; }}
    .badge span:first-child {{ color: #0088cc; margin-right: 6px; }}
    .hero {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 24px; }}
    @media (max-width: 600px) {{ .hero {{ grid-template-columns: 1fr; }} }}
    .card {{ background: #fff; border: 1px solid #e1e8ed; padding: 24px; border-radius: 12px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }}
    h1 {{ font-size: 28px; margin: 0 0 16px; line-height: 1.2; }}
    h2 {{ font-size: 18px; margin: 24px 0 12px; color: #555; text-transform: uppercase; letter-spacing: 0.5px; }}
    p {{ color: #444; margin: 0 0 16px; }}
    .btn {{ display: inline-block; background: #0088cc; color: #fff; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: 600; transition: background 0.2s; }}
    .btn:hover {{ background: #0077b5; }}
    .price {{ display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #f0f0f0; }}
    .price b {{ font-size: 16px; }}
    .price span {{ font-weight: 700; color: #0088cc; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="badge">🎙️ <span>Lingovox</span> <span>— AI Voice Translator for Telegram</span></div>

    <div class="hero">
      <div class="card">
        <h1>Translate voice messages — instantly.</h1>
        <p>
          Lingovox is a Telegram bot that converts your voice message to text, translates it to your selected language, 
          and replies with a natural-sounding voice message.
        </p>
        <div style="margin-top:12px;">
          {f'<a class="btn" href="{bot_link}" target="_blank">Open Telegram bot ↗</a>' if bot_link else '<span>Configure BOT_USERNAME</span>'}
        </div>

        <h2>Supported languages</h2>
        <p>{langs_list}</p>

        <h2>How it works</h2>
        <p>1. Select target language<br>2. Send voice message<br>3. Receive translated voice back</p>
      </div>

      <div class="card">
        <h2>Pricing (credits)</h2>
        <div class="price"><b>30 minutes</b><span>$10</span></div>
        <div class="price"><b>60 minutes</b><span>$15</span></div>
        <div class="price"><b>180 minutes</b><span>$30</span></div>
        <div class="price"><b>600 minutes</b><span>$70</span></div>

        <h2 style="margin-top:18px;">Free trial</h2>
        <p>New users get <b>{TRIAL_LIMIT}</b> free messages (up to <b>{TRIAL_MAX_SECONDS}s</b> each).</p>

        <h2 style="margin-top:18px;">Integration</h2>
        <p>Secure payments powered by <b>Paddle</b>[cite: 1].</p>
      </div>
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)

# ============================
# Bot Logic: Основные функции
# ============================
@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    keyboard = InlineKeyboardMarkup(row_width=2)
    buttons = [InlineKeyboardButton(text=name, callback_data=f"set_lang_{code}") for name, code in LANGS]
    keyboard.add(*buttons)
    await message.answer("Welcome to Lingovox! Please select the target language for translation:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith('set_lang_'))
async def process_language_selection(callback_query: types.CallbackQuery):
    lang_code = callback_query.data.split('_')[-1]
    lang_name = LANG_LABELS.get(lang_code, lang_code)
    # Здесь должна быть логика сохранения выбора пользователя в БД
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(callback_query.from_user.id, f"Target language set to: **{lang_name}**")

# Запуск через Webhook или Polling (для примера polling)
if __name__ == '__main__':
    import asyncio
    from aiogram import executor
    executor.start_polling(dp, skip_updates=True)
