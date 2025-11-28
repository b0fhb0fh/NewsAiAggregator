#!/usr/bin/python3

import telebot
from telethon import TelegramClient, events
from telethon.tl import types
from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError
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

def format_text(text, source=None):
    """
    Форматирует текст для Telegram с HTML-разметкой
    Args:
        text: Исходный текст
        source: Имя источника (добавляется в подпись)
    Returns:
        str: Отформатированный текст с HTML-тегами
    """
    if not text:
        return f"<b>📷 Медиа из @{source}</b>" if source else ""
    
    # Удаляем Markdown-разметку (**) если есть
    text = text.replace("**", "").replace("__", "")
    
    # Добавляем HTML-теги
    formatted_text = f"<b>{text.strip()}</b>"
    
    # Добавляем источник если указан
    if source:
        formatted_text += f"\n\n<b>Источник:</b> @{source}"
    
    return formatted_text

# Функция для отправки медиафайлов в целевой канал
async def send_media_to_channel(chat_username, event):
    """Безопасная отправка медиа с обработкой всех ошибок"""
    async def download_media_to_buffer():
        buffer = BytesIO()
        try:
            await event.download_media(file=buffer)
            buffer.seek(0)
            if buffer.getbuffer().nbytes == 0:
                raise ValueError("Получен пустой файл")
            return buffer
        except Exception as e:
            buffer.close()
            raise

    def sync_send_media(buffer, caption, media_type):
        """Синхронная отправка медиа"""
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
        finally:
            buffer.close()

    try:
        message = event.message
        caption = format_text(message.text or "", chat_username)

        # Обработка текстового сообщения без медиа
        if not hasattr(message, 'media') or not message.media:
            if message.text:
                await asyncio.to_thread(
                    lambda: bot.send_message(
                        chat_id=SUMMARY_CHANNEL_ID,
                        text=caption,
                        parse_mode="HTML"
                    )
                )
            return

        # Определение типа медиа
        if isinstance(message.media, types.MessageMediaPhoto):
            media_type = 'photo'
        elif isinstance(message.media, types.MessageMediaDocument):
            media_type = 'document'
        else:
            logging.warning(f"Неподдерживаемый тип медиа: {type(message.media)}")
            return

        # Загрузка и отправка медиа
        buffer = await download_media_to_buffer()
        await asyncio.to_thread(sync_send_media, buffer, caption, media_type)

    except Exception as e:
        logging.error(f"Ошибка отправки: {str(e)}", exc_info=True)
        # Отправка текста как запасного варианта
        if hasattr(message, 'text') and message.text:
            await asyncio.to_thread(
                lambda: bot.send_message(
                    chat_id=SUMMARY_CHANNEL_ID,
                    text=f"⚠️ Ошибка вложения\n\n{format_text(message.text, chat_username)}",
                    parse_mode="HTML"
                )
            )
            
# Обработчик новых сообщений из каналов
async def handle_new_message(event):
    try:
        chat = event.chat
        chat_username = chat.username if chat and chat.username else f"id{chat.id}" if chat else "unknown"

        logging.info(f"Новое сообщение из {chat_username}")
        
        message_text = event.message.text or ""
        is_relevant = check_topic_relevance(message_text) if message_text else True
        
        if is_relevant:
            await send_media_to_channel(chat_username, event)

    except Exception as e:
        logging.error(f"Ошибка обработки: {str(e)}", exc_info=True)

# Функция для валидации и фильтрации каналов
async def validate_channels(channels):
    """Валидирует список каналов и возвращает только валидные"""
    valid_channels = []
    invalid_channels = []
    
    for channel in channels:
        try:
            # Пытаемся получить entity канала
            entity = await client.get_entity(channel)
            valid_channels.append(entity)
            logging.info(f"Канал {channel} успешно валидирован")
        except (UsernameInvalidError, UsernameNotOccupiedError, ValueError) as e:
            invalid_channels.append(channel)
            logging.warning(f"Канал {channel} невалиден или недоступен: {e}")
        except Exception as e:
            invalid_channels.append(channel)
            logging.error(f"Ошибка при валидации канала {channel}: {e}")
    
    if invalid_channels:
        logging.warning(f"Следующие каналы будут пропущены: {', '.join(invalid_channels)}")
    
    return valid_channels

async def run_telethon():
    try:
        logging.info("Запуск Telethon клиента...")
        await client.start(PHONE_NUMBER)
        logging.info("Telethon клиент успешно запущен.")
        
        # Валидируем каналы и регистрируем обработчик только для валидных
        logging.info(f"Валидация {len(CHANNELS_TO_MONITOR)} каналов...")
        valid_channels = await validate_channels(CHANNELS_TO_MONITOR)
        
        if not valid_channels:
            logging.error("Нет валидных каналов для мониторинга!")
            return
        
        logging.info(f"Регистрация обработчика для {len(valid_channels)} валидных каналов")
        
        # Регистрируем обработчик для валидных каналов
        @client.on(events.NewMessage(chats=valid_channels))
        async def message_handler(event):
            await handle_new_message(event)
        
        logging.info("Обработчик сообщений успешно зарегистрирован")
        
        # Ждем флаг завершения
        while not shutdown:
            await asyncio.sleep(1)
            
    except Exception as e:
        logging.error(f"Ошибка в Telethon клиенте: {e}", exc_info=True)
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