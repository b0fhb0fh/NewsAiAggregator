#!/usr/bin/python3

import telebot
from telethon import TelegramClient, events
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
            f"Ответь только 'Да' или 'Нет'.\n\nСообщение: {text}\n\n Если сообщение носит рекламный характер, отвечай 'Нет'."
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
    """
    Отправляет медиафайлы или текст в целевой канал с обработкой всех типов контента.
    
    Параметры:
        chat_username: Имя пользователя или ID чата-источника
        message: Объект сообщения от aiogram
        
    Обрабатывает:
        - Фото
        - Видео
        - Документы
        - Аудио
        - Голосовые сообщения
        - Текстовые сообщения
        - Стикеры
        - Анимации (GIF)
    """
    try:
        logging.info(f"Попытка отправить контент из {chat_username}")
        
        # Безопасное формирование подписи
        caption = (
            getattr(message, 'caption', None)  # Пробуем получить caption (если есть)
            or message.text  # Или текст сообщения
            or f"📷 Медиа из @{chat_username}"  # Или заглушка
        )
        
        # Определяем тип контента и отправляем
        if message.photo:
            await bot.send_photo(
                chat_id=SUMMARY_CHANNEL_ID,
                photo=message.photo[-1].file_id,
                caption=caption[:1024]  # Ограничение длины подписи в Telegram
            )
            
        elif message.video:
            await bot.send_video(
                chat_id=SUMMARY_CHANNEL_ID,
                video=message.video.file_id,
                caption=caption[:1024]
            )
            
        elif message.document:
            await bot.send_document(
                chat_id=SUMMARY_CHANNEL_ID,
                document=message.document.file_id,
                caption=caption[:1024]
            )
            
        elif message.audio:
            await bot.send_audio(
                chat_id=SUMMARY_CHANNEL_ID,
                audio=message.audio.file_id,
                caption=caption[:1024]
            )
            
        elif message.voice:
            await bot.send_voice(
                chat_id=SUMMARY_CHANNEL_ID,
                voice=message.voice.file_id,
                caption=caption[:1024] if caption else None
            )
            
        elif message.sticker:
            await bot.send_sticker(
                chat_id=SUMMARY_CHANNEL_ID,
                sticker=message.sticker.file_id
            )
            
        elif message.animation:
            await bot.send_animation(
                chat_id=SUMMARY_CHANNEL_ID,
                animation=message.animation.file_id,
                caption=caption[:1024]
            )
            
        elif message.text:
            await bot.send_message(
                chat_id=SUMMARY_CHANNEL_ID,
                text=message.text
            )
            
        logging.info(f"Контент из {chat_username} успешно отправлен")
        
    except Exception as e:
        logging.error(f"Ошибка при отправке контента из {chat_username}: {str(e)}")
        raise  # Пробрасываем исключение для обработки выше

# Обработчик новых сообщений из каналов
@client.on(events.NewMessage)
async def handle_new_message(event):
    try:
        # Проверяем, что сообщение из одного из каналов в списке
        chat_username = event.chat.username if event.chat and event.chat.username else str(event.chat.id) if event.chat else "unknown"
        if not event.chat:
            logging.warning(f"Сообщение без чата! Content: {event.text or 'Media'}")
        else:
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
