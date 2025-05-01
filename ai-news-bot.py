#!/usr/bin/python3

import telebot
from telethon import TelegramClient, events
from telethon.tl import types
import io
import requests
import json
import logging
import asyncio
import sys
import os
import time
import signal
import threading
from threading import Event
from io import BytesIO
import asyncio

# Флаг для graceful shutdown
shutdown = False

def signal_handler(sig, frame):
    global shutdown
    logging.info("Получен сигнал завершения, начинаем shutdown...")
    shutdown = True

# Регистрация обработчика сигналов
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# Загрузка конфигурации
try:
    with open("config.json", "r") as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    print("Ошибка: файл config.json не найден.")
    sys.exit(1)
except json.JSONDecodeError:
    print("Ошибка: файл config.json имеет неверный формат.")
    sys.exit(1)

# Конфигурация (обязательные параметры)
TELEGRAM_BOT_TOKEN = config["TELEGRAM_BOT_TOKEN"]
SUMMARY_CHANNEL_ID = config["SUMMARY_CHANNEL_ID"]
API_ID = config["API_ID"]
API_HASH = config["API_HASH"]
PHONE_NUMBER = config["PHONE_NUMBER"]
OLLAMA_URL = config["OLLAMA_URL"]
OLLAMA_MODEL = config["OLLAMA_MODEL"]
INTEREST_TOPICS = config["INTEREST_TOPICS"]
CHANNELS_TO_MONITOR = config["CHANNELS_TO_MONITOR"]

# Необязательные параметры (с значениями по умолчанию)
CHECK_INTERVAL = config.get("CHECK_INTERVAL", 300)  # Значение по умолчанию: 300 секунд
LOG_LEVEL = config.get("LOG_LEVEL", "INFO")  # Значение по умолчанию: INFO

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()  # Вывод логов в консоль
    ]
)

# Инициализация бота
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Инициализация Telethon клиента
client = TelegramClient('session_name', API_ID, API_HASH)

# Функция для проверки принадлежности сообщения к интересуемым темам
def check_topic_relevance(text):
    logging.info("Начало проверки релевантности сообщения.")
    try:
        # Формируем запрос к Ollama
        prompt = (
            f"Прочитай это сообщение и определи, относится ли оно к одной из этих тем: {', '.join(INTEREST_TOPICS)}. "
            f"Ответь только 'Да' или 'Нет'.\n\nСообщение: {text}"
        )
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False
        }
        logging.info(f"Отправка запроса к Ollama: {payload}")
        response = requests.post(OLLAMA_URL, json=payload)
        if response.status_code == 200:
            verdict = response.json()["response"].strip().lower()
            logging.info(f"Ответ от Ollama: {verdict}")
            return "да" in verdict  # Проверяем наличие подстроки "да"
        else:
            logging.error(f"Ошибка при запросе к Ollama: {response.status_code}")
            return False
    except Exception as e:
        logging.error(f"Ошибка в функции check_topic_relevance: {e}")
        return False

# Функция для отправки медиафайлов в целевой канал
async def send_media_to_channel(chat_username, event):
    """Надежная отправка медиа через временный буфер в памяти"""
    async def download_media_to_buffer():
        buffer = BytesIO()
        await event.download_media(file=buffer)
        buffer.seek(0)
        return buffer

    def sync_send(buffer, caption, media_type):
        try:
            if media_type == 'photo':
                bot.send_photo(
                    chat_id=SUMMARY_CHANNEL_ID,
                    photo=buffer,
                    caption=caption,
                    parse_mode="HTML"
                )
            elif media_type == 'document':
                bot.send_document(
                    chat_id=SUMMARY_CHANNEL_ID,
                    document=buffer,
                    caption=caption,
                    parse_mode="HTML"
                )
            buffer.close()
        except Exception as e:
            logging.error(f"Ошибка отправки: {str(e)}")
            buffer.close()
            raise

    try:
        message = event.message
        caption = (message.text or f"📷 Медиа из @{chat_username}")

        if message.media:
            # Загружаем медиа в память
            buffer = await download_media_to_buffer()
            
            # Определяем тип медиа
            if isinstance(message.media, types.MessageMediaPhoto):
                await asyncio.to_thread(sync_send, buffer, caption, 'photo')
            else:
                await asyncio.to_thread(sync_send, buffer, caption, 'document')

        elif message.text:
            await asyncio.to_thread(
                lambda: bot.send_message(
                    chat_id=SUMMARY_CHANNEL_ID,
                    text=caption,
                    parse_mode="HTML"
                )
            )

    except Exception as e:
        logging.error(f"Фатальная ошибка: {str(e)}", exc_info=True)

# Обработчик новых сообщений из каналов
@client.on(events.NewMessage(chats=CHANNELS_TO_MONITOR))
async def handle_new_message(event):
    try:
        chat = event.chat
        chat_username = chat.username if chat and chat.username else f"id{chat.id}" if chat else "unknown"
        
        logging.info(f"Новое сообщение из {chat_username}")
        
        # Проверка релевантности (ваш существующий код)
        message_text = event.message.text or ""
        is_relevant = check_topic_relevance(message_text) if message_text else True
        
        if is_relevant:
            await send_media_to_channel(chat_username, event)  # Передаем event, а не message
            
    except Exception as e:
        logging.error(f"Ошибка обработки: {str(e)}", exc_info=True)

async def run_telethon():
    try:
        logging.info("Запуск Telethon клиента...")
        await client.start(PHONE_NUMBER)
        logging.info("Telethon клиент успешно запущен.")
        
        # Ждем флаг завершения
        while not shutdown:
            await asyncio.sleep(1)
            
    except Exception as e:
        logging.error(f"Ошибка в Telethon клиенте: {e}")
    finally:
        await client.disconnect()
        logging.info("Telethon клиент остановлен.")

def run_bot():
    try:
        logging.info("Запуск бота...")
        bot.polling(none_stop=True, interval=1)
    except Exception as e:
        logging.error(f"Ошибка в боте: {e}")
    finally:
        logging.info("Бот остановлен")

async def main():
    # Запускаем Telethon в отдельной задаче
    telethon_task = asyncio.create_task(run_telethon())
    
    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Основной цикл
    try:
        while not shutdown:
            await asyncio.sleep(1)
    finally:
        # Останавливаем задачи
        telethon_task.cancel()
        try:
            await telethon_task
        except asyncio.CancelledError:
            pass
        
        # Останавливаем бота
        bot.stop_polling()
        bot_thread.join(timeout=2)
        logging.info("Приложение полностью остановлено")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)