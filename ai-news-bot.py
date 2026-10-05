#!/usr/bin/python3

import telebot
from telethon import TelegramClient, events
from telethon.tl import types
from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError
import io
import html
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

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
ELLIPSIS = "..."

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

def is_positive_verdict(verdict):
    """Вердикт положительный, если первое слово — «да» или «yes»."""
    stripped = verdict.strip().lower()
    if not stripped:
        return False
    first_word = stripped.split(None, 1)[0].strip(".,!?;:\"'«»()[]")
    return first_word in {"да", "yes"}

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
            verdict = response.json()["response"].strip()
            logging.info(f"Ответ от Ollama: {verdict}")
            return is_positive_verdict(verdict)
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

def get_source_name(chat):
    if chat.username:
        return f"@{chat.username}"
    if hasattr(chat, "title") and chat.title:
        return chat.title
    return f"Канал {chat.id}"

def format_source_parts(chat, message_id):
    """
    Возвращает видимый суффикс источника и HTML-версию со ссылкой.
    HTML остаётся только у ссылки, имя канала экранируется.
    """
    message_link = get_message_link(chat, message_id)
    source_name = get_source_name(chat)
    visible = f"\n\n🔗 Источник: {source_name}"
    html_part = f'\n\n🔗 <a href="{message_link}">Источник: {escape_html(source_name)}</a>'
    return visible, html_part

def utf16_len(text):
    """Длина строки в единицах UTF-16, как считает Telegram."""
    return len(text.encode("utf-16-le")) // 2

def escape_html(text):
    return html.escape(text, quote=False)

def slice_utf16(text, max_units):
    """Делит текст так, чтобы первая часть занимала не больше max_units UTF-16."""
    if max_units <= 0:
        return "", text
    if utf16_len(text) <= max_units:
        return text, ""
    used = 0
    cut = 0
    for i, ch in enumerate(text):
        ch_units = 2 if ord(ch) > 0xFFFF else 1
        if used + ch_units > max_units:
            break
        used += ch_units
        cut = i + 1
    return text[:cut], text[cut:]

def build_media_caption(text, source_visible, source_html):
    """
    Собирает подпись медиа в лимите 1024.
    Возвращает (caption_html, хвост исходного текста).
    Ссылка на источник всегда остаётся в подписи.
    """
    if not text:
        return source_html, ""

    if utf16_len(text + source_visible) <= CAPTION_LIMIT:
        return escape_html(text) + source_html, ""

    reserved = utf16_len(ELLIPSIS + source_visible)
    available = CAPTION_LIMIT - reserved
    if available <= 0:
        return source_html, text

    head, rest = slice_utf16(text, available)
    if not rest:
        return escape_html(head) + source_html, ""
    return escape_html(head) + ELLIPSIS + source_html, rest

def iter_text_chunks(text, source_visible, source_html, include_source=True):
    """Режет текст на HTML-куски в лимите обычного сообщения."""
    if include_source:
        if not text:
            yield source_html
            return
        if utf16_len(text + source_visible) <= MESSAGE_LIMIT:
            yield escape_html(text) + source_html
            return
        reserved = utf16_len(source_visible)
        available = MESSAGE_LIMIT - reserved
        if available <= 0:
            yield source_html
            remaining = text
        else:
            head, remaining = slice_utf16(text, available)
            yield escape_html(head) + source_html
    else:
        remaining = text

    while remaining:
        part, remaining = slice_utf16(remaining, MESSAGE_LIMIT)
        yield escape_html(part)

async def send_html_messages(chunks, reply_to_message_id=None, disable_web_page_preview=False):
    last = None
    for chunk in chunks:
        last = await asyncio.to_thread(
            bot.send_message,
            chat_id=SUMMARY_CHANNEL_ID,
            text=chunk,
            parse_mode="HTML",
            disable_web_page_preview=disable_web_page_preview,
            reply_to_message_id=reply_to_message_id,
        )
    return last

async def send_media_buffer(message, buffer, caption):
    buffer.seek(0)
    media = message.media
    if isinstance(media, types.MessageMediaPhoto):
        return await asyncio.to_thread(
            bot.send_photo,
            chat_id=SUMMARY_CHANNEL_ID,
            photo=buffer,
            caption=caption,
            parse_mode="HTML",
        )
    if isinstance(media, types.MessageMediaDocument):
        doc = media.document
        mime_type = getattr(doc, "mime_type", None) if doc else None
        if mime_type and mime_type.startswith("video/"):
            return await asyncio.to_thread(
                bot.send_video,
                chat_id=SUMMARY_CHANNEL_ID,
                video=buffer,
                caption=caption,
                parse_mode="HTML",
            )
        return await asyncio.to_thread(
            bot.send_document,
            chat_id=SUMMARY_CHANNEL_ID,
            document=buffer,
            caption=caption,
            parse_mode="HTML",
        )
    return None

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
        text = message.text or ""
        source_visible, source_html = format_source_parts(chat, message_id)

        has_media = hasattr(message, "media") and message.media
        is_supported_media = has_media and isinstance(
            message.media, (types.MessageMediaPhoto, types.MessageMediaDocument)
        )

        if not has_media:
            chunks = iter_text_chunks(text or "📎 Медиа без текста", source_visible, source_html)
            await send_html_messages(chunks)
            return

        if not is_supported_media:
            logging.warning(f"Неподдерживаемый тип медиа: {type(message.media)}")
            fallback = f"{text}\n\n⚠️ Неподдерживаемый тип медиа" if text else "⚠️ Неподдерживаемый тип медиа"
            await send_html_messages(iter_text_chunks(fallback, source_visible, source_html))
            return

        buffer = BytesIO()
        try:
            try:
                await message.download_media(file=buffer)
                buffer.seek(0)
                if buffer.getbuffer().nbytes == 0:
                    raise ValueError("Получен пустой файл")
            except Exception as download_error:
                logging.error(
                    f"Ошибка при загрузке медиа сообщения {message_id}: {download_error}",
                    exc_info=True,
                )
                fallback = f"{text}\n\n⚠️ Не удалось загрузить медиа" if text else "⚠️ Не удалось загрузить медиа"
                await send_html_messages(iter_text_chunks(fallback, source_visible, source_html))
                return

            caption, remainder = build_media_caption(text, source_visible, source_html)
            if remainder:
                logging.info(
                    f"Подпись сообщения {message_id} превышает {CAPTION_LIMIT}, "
                    "хвост текста отправим ответом"
                )

            try:
                sent = await send_media_buffer(message, buffer, caption)
                if remainder and sent is not None:
                    await send_html_messages(
                        iter_text_chunks(remainder, source_visible, source_html, include_source=False),
                        reply_to_message_id=sent.message_id,
                        disable_web_page_preview=True,
                    )
            except Exception as send_error:
                logging.error(
                    f"Ошибка при отправке медиа сообщения {message_id}: {send_error}",
                    exc_info=True,
                )
                fallback = f"{text}\n\n⚠️ Не удалось отправить медиа" if text else "⚠️ Не удалось отправить медиа"
                await send_html_messages(iter_text_chunks(fallback, source_visible, source_html))
        finally:
            buffer.close()

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
        
        # Проверка релевантности — синхронный запрос к Ollama в отдельном потоке
        message_text = message.text or ""
        is_relevant = (
            await asyncio.to_thread(check_topic_relevance, message_text)
            if message_text
            else True
        )
        
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