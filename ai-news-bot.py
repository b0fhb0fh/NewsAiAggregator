#!/usr/bin/python3

import telebot
from telethon import TelegramClient, events
import requests
import json
import logging
import asyncio
import sys
import os

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
async def send_media_to_channel(chat_username, message):
    try:
        logging.info(f"Попытка отправить медиафайл из {chat_username}.")
        if message.photo:  # Если сообщение содержит фото
            file_id = message.photo[-1].file_id  # Берем самое большое фото
            await bot.send_photo(SUMMARY_CHANNEL_ID, file_id, caption=message.text if message.text else f"📷 Фото из {chat_username}")

        elif message.video:  # Если сообщение содержит видео
            file_id = message.video.file_id
            await bot.send_video(SUMMARY_CHANNEL_ID, file_id, caption=message.text if message.text else f"🎥 Видео из {chat_username}")

        elif message.document:  # Если сообщение содержит документ
            file_id = message.document.file_id
            await bot.send_document(SUMMARY_CHANNEL_ID, file_id, caption=message.text if message.text else f"📄 Документ из {chat_username}")

        elif message.audio:  # Если сообщение содержит аудио
            file_id = message.audio.file_id
            await bot.send_audio(SUMMARY_CHANNEL_ID, file_id, caption=message.text if message.text else f"🎵 Аудио из {chat_username}")

        elif message.voice:  # Если сообщение содержит голосовое сообщение
            file_id = message.voice.file_id
            await bot.send_voice(SUMMARY_CHANNEL_ID, file_id, caption=message.text if message.text else f"🎤 Голосовое сообщение из {chat_username}")

        logging.info(f"Медиафайл из {chat_username} отправлен.")
    except Exception as e:
        logging.error(f"Ошибка при отправке медиафайла: {e}")

# Обработчик новых сообщений из каналов
@client.on(events.NewMessage)
async def handle_new_message(event):
    try:
        # Проверяем, что сообщение из одного из каналов в списке
        chat_username = event.chat.username if event.chat.username else str(event.chat.id)
        logging.info(f"Получено сообщение из {chat_username}.")
        
        if chat_username in CHANNELS_TO_MONITOR:
            logging.info(f"Сообщение из {chat_username} находится в списке для мониторинга.")
            
            # Получаем текст сообщения с форматированием (включая гиперссылки)
            message_text = event.message.text if event.message.text else ""
            logging.info(f"Текст сообщения: {message_text}")

            # Если есть текст, проверяем его на принадлежность к интересуемым темам
            if message_text:
                logging.info("Проверка текста на релевантность.")
                is_relevant = check_topic_relevance(message_text)
                logging.info(f"Сообщение относится к интересуемым темам: {is_relevant}")
            else:
                # Если текста нет, считаем сообщение релевантным (например, только медиа)
                is_relevant = True
                logging.info("Сообщение не содержит текста, считается релевантным.")

            if is_relevant:
                # Если есть медиафайлы, отправляем их
                if event.message.media:
                    logging.info("Сообщение содержит медиафайлы.")
                    await send_media_to_channel(chat_username, event.message)

                # Если есть текст, отправляем его
                if message_text:
                    try:
                        logging.info("Попытка отправить текстовое сообщение.")
                        await bot.send_message(
                            SUMMARY_CHANNEL_ID,
                            message_text,
                            parse_mode="HTML"  # Сохраняем форматирование
                        )
                        logging.info(f"Сообщение из {chat_username} опубликовано.")
                    except Exception as e:
                        logging.error(f"Ошибка при публикации текстового сообщения: {e}")
            else:
                logging.info(f"Сообщение из {chat_username} не соответствует темам.")
        else:
            logging.info(f"Сообщение из {chat_username} не в списке для мониторинга.")
    except Exception as e:
        logging.error(f"Ошибка в обработчике handle_new_message: {e}")

# Запуск Telethon клиента
async def start_telethon_client():
    try:
        logging.info("Запуск Telethon клиента...")
        await client.start(PHONE_NUMBER)
        logging.info("Telethon клиент успешно запущен.")
        await client.run_until_disconnected()
    except Exception as e:
        logging.error(f"Ошибка при запуске Telethon клиента: {e}")

# Запуск бота
if __name__ == "__main__":
    try:
        logging.info("Запуск бота...")
        # Запускаем Telethon клиент в отдельном потоке
        import threading
        telethon_thread = threading.Thread(target=lambda: asyncio.run(start_telethon_client()))
        telethon_thread.start()

        # Запускаем Telegram-бота
        logging.info("Telegram-бот запущен.")
        bot.polling(none_stop=True)
    except Exception as e:
        logging.error(f"Ошибка при запуске бота: {e}")
