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

def get_message_link(chat, message_id):
    """
    Создает прямую ссылку на сообщение
    Args:
        chat: Объект чата из Telethon
        message_id: ID сообщения
    Returns:
        str: Ссылка на сообщение в формате t.me/channel/message_id
    """
    if chat.username:
        return f"https://t.me/{chat.username}/{message_id}"
    else:
        # Для каналов без username используем c/format
        return f"https://t.me/c/{str(chat.id)[4:]}/{message_id}"

def format_source_info(chat, message_id):
    """
    Форматирует информацию об источнике с ссылкой
    Args:
        chat: Объект чата из Telethon
        message_id: ID сообщения
    Returns:
        str: Отформатированная строка с источником и ссылкой
    """
    message_link = get_message_link(chat, message_id)
    if chat.username:
        source_name = f"@{chat.username}"
    else:
        source_name = chat.title if hasattr(chat, 'title') and chat.title else f"Канал {chat.id}"
    
    return f"\n\n🔗 <a href=\"{message_link}\">Источник: {source_name}</a>"

# Функция для отправки сообщений в целевой канал
async def send_message_to_channel(event):
    """
    Отправляет сообщение в целевой канал используя копирование через Bot API
    Это гарантирует, что сообщения будут отображаться как непрочитанные
    """
    try:
        message = event.message
        chat = event.chat
        message_id = message.id
        
        # Используем копирование через Bot API вместо forward
        # Сообщения от бота будут отображаться как непрочитанные
        logging.info(f"Копирование сообщения {message_id} из {chat.id} в целевой канал")
        await copy_message_to_channel(event)
            
    except Exception as e:
        logging.error(f"Критическая ошибка при отправке сообщения: {str(e)}", exc_info=True)

async def copy_message_to_channel(event):
    """
    Копирует сообщение в целевой канал с сохранением всех медиа и ссылок
    Сообщения отправляются через Bot API и отображаются как непрочитанные
    """
    try:
        message = event.message
        chat = event.chat
        message_id = message.id
        
        # Получаем ссылку на исходное сообщение
        source_info = format_source_info(chat, message_id)
        
        # Обработка текстового сообщения без медиа
        if not hasattr(message, 'media') or not message.media:
            text = message.text or ""
            # Сохраняем оригинальный текст без изменений
            if text:
                full_text = text + source_info
                await asyncio.to_thread(
                    lambda: bot.send_message(
                        chat_id=SUMMARY_CHANNEL_ID,
                        text=full_text,
                        parse_mode="HTML",
                        disable_web_page_preview=False
                    )
                )
            else:
                # Если нет текста, отправляем только ссылку на источник
                await asyncio.to_thread(
                    lambda: bot.send_message(
                        chat_id=SUMMARY_CHANNEL_ID,
                        text=f"📎 Медиа без текста{source_info}",
                        parse_mode="HTML"
                    )
                )
            return

        # Обработка медиа
        try:
            # Загружаем медиа в буфер
            buffer = BytesIO()
            await message.download_media(file=buffer)
            buffer.seek(0)
            
            if buffer.getbuffer().nbytes == 0:
                raise ValueError("Получен пустой файл")
            
            # Определяем тип медиа и текст подписи
            caption = (message.text or "") + source_info if message.text else source_info
            
            # Определение типа медиа
            if isinstance(message.media, types.MessageMediaPhoto):
                await asyncio.to_thread(
                    lambda: bot.send_photo(
                        chat_id=SUMMARY_CHANNEL_ID,
                        photo=buffer,
                        caption=caption if caption else None,
                        parse_mode="HTML"
                    )
                )
            elif isinstance(message.media, types.MessageMediaDocument):
                # Проверяем, является ли документ видео или другим типом
                doc = message.media.document
                mime_type = None
                if doc and hasattr(doc, 'mime_type'):
                    mime_type = doc.mime_type
                
                if mime_type and mime_type.startswith('video/'):
                    await asyncio.to_thread(
                        lambda: bot.send_video(
                            chat_id=SUMMARY_CHANNEL_ID,
                            video=buffer,
                            caption=caption if caption else None,
                            parse_mode="HTML"
                        )
                    )
                else:
                    await asyncio.to_thread(
                        lambda: bot.send_document(
                            chat_id=SUMMARY_CHANNEL_ID,
                            document=buffer,
                            caption=caption if caption else None,
                            parse_mode="HTML"
                        )
                    )
            else:
                logging.warning(f"Неподдерживаемый тип медиа: {type(message.media)}")
                # Отправляем текст с ссылкой на источник
                text = message.text or ""
                if text:
                    await asyncio.to_thread(
                        lambda: bot.send_message(
                            chat_id=SUMMARY_CHANNEL_ID,
                            text=f"{text}\n\n⚠️ Неподдерживаемый тип медиа{source_info}",
                            parse_mode="HTML"
                        )
                    )
            
            buffer.close()
            
        except Exception as media_error:
            logging.error(f"Ошибка при обработке медиа сообщения {message_id}: {media_error}", exc_info=True)
            # Отправляем текст как запасной вариант
            text = message.text or ""
            if text:
                await asyncio.to_thread(
                    lambda: bot.send_message(
                        chat_id=SUMMARY_CHANNEL_ID,
                        text=f"{text}\n\n⚠️ Не удалось загрузить медиа{source_info}",
                        parse_mode="HTML"
                    )
                )
            else:
                await asyncio.to_thread(
                    lambda: bot.send_message(
                        chat_id=SUMMARY_CHANNEL_ID,
                        text=f"⚠️ Ошибка при обработке медиа{source_info}",
                        parse_mode="HTML"
                    )
                )
            
    except Exception as e:
        logging.error(f"Ошибка при копировании сообщения: {str(e)}", exc_info=True)
            
# Обработчик новых сообщений из каналов
async def handle_new_message(event):
    try:
        chat = event.chat
        message = event.message
        message_id = message.id
        
        # Определяем имя канала для логирования
        if chat.username:
            chat_name = f"@{chat.username}"
        elif hasattr(chat, 'title') and chat.title:
            chat_name = chat.title
        else:
            chat_name = f"id{chat.id}"

        logging.info(f"Новое сообщение {message_id} из {chat_name}")
        
        # Проверка релевантности
        message_text = message.text or ""
        is_relevant = check_topic_relevance(message_text) if message_text else True
        
        if is_relevant:
            await send_message_to_channel(event)
        else:
            logging.debug(f"Сообщение {message_id} не релевантно, пропускаем")

    except Exception as e:
        logging.error(f"Ошибка обработки сообщения: {str(e)}", exc_info=True)

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