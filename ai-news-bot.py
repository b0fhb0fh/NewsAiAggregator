#!/usr/bin/python3

import telebot
from telebot.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo
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
OLLAMA_TIMEOUT = config.get("OLLAMA_TIMEOUT", 30)
OLLAMA_FAIL_COOLDOWN = config.get("OLLAMA_FAIL_COOLDOWN", 60)
# Если Ollama недоступна — публикуем, а не молча отбрасываем все новости
PUBLISH_ON_OLLAMA_ERROR = config.get("PUBLISH_ON_OLLAMA_ERROR", True)

_ollama_unavailable_until = 0.0

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
bot_send_lock = asyncio.Lock()

# Инициализация Telethon клиента
client = TelegramClient('session_name', API_ID, API_HASH)

def is_positive_verdict(verdict):
    """Вердикт положительный, если первое слово — «да» или «yes»."""
    stripped = verdict.strip().lower()
    if not stripped:
        return False
    first_word = stripped.split(None, 1)[0].strip(".,!?;:\"'«»()[]")
    return first_word in {"да", "yes"}

def _ollama_error_decision(reason):
    """При сбое фильтра не теряем новости: по умолчанию публикуем."""
    global _ollama_unavailable_until
    _ollama_unavailable_until = time.time() + OLLAMA_FAIL_COOLDOWN
    if PUBLISH_ON_OLLAMA_ERROR:
        logging.warning(
            f"{reason}. Публикуем без фильтра, следующая попытка Ollama через "
            f"{OLLAMA_FAIL_COOLDOWN} с."
        )
        return True
    logging.error(f"{reason}. Сообщение пропущено (PUBLISH_ON_OLLAMA_ERROR=false).")
    return False

# Функция для проверки принадлежности сообщения к интересуемым темам
def check_topic_relevance(text):
    global _ollama_unavailable_until
    if time.time() < _ollama_unavailable_until:
        if PUBLISH_ON_OLLAMA_ERROR:
            logging.warning("Ollama ещё недоступна, публикуем без фильтра.")
            return True
        return False

    logging.info("Начало проверки релевантности сообщения.")
    try:
        prompt = (
            f"Прочитай это сообщение и определи, относится ли оно к одной из этих тем: {', '.join(INTEREST_TOPICS)}. "
            f"Ответь только 'Да' или 'Нет'.\n\nСообщение: {text}"
        )
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False
        }
        logging.info(f"Запрос к Ollama: model={OLLAMA_MODEL}, text_len={len(text)}")
        response = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
        if response.status_code == 200:
            _ollama_unavailable_until = 0.0
            verdict = response.json()["response"].strip()
            logging.info(f"Ответ от Ollama: {verdict}")
            return is_positive_verdict(verdict)
        return _ollama_error_decision(f"Ollama вернула HTTP {response.status_code}")
    except Exception as e:
        return _ollama_error_decision(f"Ollama недоступна ({e})")

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

def get_message_text(message):
    """Подпись/текст: raw_text надёжнее, чем .text (markdown)."""
    for attr in ("raw_text", "message", "text"):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""

def get_album_text(event):
    for attr in ("raw_text", "text"):
        value = getattr(event, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    for message in getattr(event, "messages", None) or []:
        text = get_message_text(message)
        if text:
            return text
    return ""

def media_kind(message):
    media = getattr(message, "media", None)
    if isinstance(media, types.MessageMediaPhoto):
        return "photo"
    if isinstance(media, types.MessageMediaDocument):
        doc = getattr(media, "document", None)
        mime_type = getattr(doc, "mime_type", None) if doc else None
        if mime_type and mime_type.startswith("video/"):
            return "video"
        return "document"
    return None

def is_downloadable_media(message):
    return media_kind(message) is not None

def attach_buffer_name(buffer, message):
    kind = media_kind(message)
    if kind == "photo":
        buffer.name = f"photo_{message.id}.jpg"
        return
    doc = getattr(getattr(message, "media", None), "document", None)
    if doc:
        for attr in getattr(doc, "attributes", []) or []:
            file_name = getattr(attr, "file_name", None)
            if file_name:
                buffer.name = file_name
                return
    ext = "mp4" if kind == "video" else "bin"
    buffer.name = f"file_{message.id}.{ext}"

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

async def bot_call(func, *args, **kwargs):
    async with bot_send_lock:
        return await asyncio.to_thread(func, *args, **kwargs)

async def send_html_messages(chunks, reply_to_message_id=None, disable_web_page_preview=False):
    last = None
    use_reply = reply_to_message_id
    for chunk in chunks:
        try:
            last = await bot_call(
                bot.send_message,
                chat_id=SUMMARY_CHANNEL_ID,
                text=chunk,
                parse_mode="HTML",
                disable_web_page_preview=disable_web_page_preview,
                reply_to_message_id=use_reply,
            )
        except Exception:
            if not use_reply:
                raise
            logging.warning("Не удалось ответить на сообщение, отправляем текст отдельно")
            last = await bot_call(
                bot.send_message,
                chat_id=SUMMARY_CHANNEL_ID,
                text=chunk,
                parse_mode="HTML",
                disable_web_page_preview=disable_web_page_preview,
            )
            use_reply = None
    return last

async def send_media_buffer(message, buffer, caption):
    attach_buffer_name(buffer, message)
    buffer.seek(0)
    kind = media_kind(message)
    extra = {}
    if caption:
        extra["caption"] = caption
        extra["parse_mode"] = "HTML"
    if kind == "photo":
        return await bot_call(bot.send_photo, chat_id=SUMMARY_CHANNEL_ID, photo=buffer, **extra)
    if kind == "video":
        return await bot_call(bot.send_video, chat_id=SUMMARY_CHANNEL_ID, video=buffer, **extra)
    if kind == "document":
        return await bot_call(bot.send_document, chat_id=SUMMARY_CHANNEL_ID, document=buffer, **extra)
    return None

def named_file_copy(buffer, message):
    attach_buffer_name(buffer, message)
    buffer.seek(0)
    copy = BytesIO(buffer.getvalue())
    copy.name = buffer.name
    return copy

def to_input_media(message, buffer, caption=None):
    extra = {}
    if caption:
        extra["caption"] = caption
        extra["parse_mode"] = "HTML"
    file_obj = named_file_copy(buffer, message)
    kind = media_kind(message)
    if kind == "photo":
        return InputMediaPhoto(file_obj, **extra)
    if kind == "video":
        return InputMediaVideo(file_obj, **extra)
    return InputMediaDocument(file_obj, **extra)

async def send_grouped_media(items, caption):
    """Отправляет 1+ файлов. Подпись только у первого. Группы по 10 — лимит Bot API."""
    sent = []
    first = True
    for start in range(0, len(items), 10):
        chunk = items[start:start + 10]
        chunk_caption = caption if first else None
        if len(chunk) == 1:
            message = await send_media_buffer(chunk[0][0], chunk[0][1], chunk_caption)
            if message is not None:
                sent.append(message)
        else:
            try:
                media = [
                    to_input_media(message, buffer, chunk_caption if i == 0 else None)
                    for i, (message, buffer) in enumerate(chunk)
                ]
                sent.extend(await bot_call(bot.send_media_group, chat_id=SUMMARY_CHANNEL_ID, media=media) or [])
            except Exception as group_error:
                logging.warning(f"send_media_group не удался, отправляем файлы по одному: {group_error}")
                for i, (message, buffer) in enumerate(chunk):
                    item = await send_media_buffer(message, buffer, chunk_caption if i == 0 else None)
                    if item is not None:
                        sent.append(item)
        first = False
    return sent

async def download_to_buffer(message):
    buffer = BytesIO()
    await message.download_media(file=buffer)
    buffer.seek(0)
    if buffer.getbuffer().nbytes == 0:
        buffer.close()
        raise ValueError("Получен пустой файл")
    attach_buffer_name(buffer, message)
    return buffer

async def send_remainder(remainder, source_visible, source_html, reply_to_message_id=None):
    if not remainder:
        return
    await send_html_messages(
        iter_text_chunks(remainder, source_visible, source_html, include_source=False),
        reply_to_message_id=reply_to_message_id,
        disable_web_page_preview=True,
    )

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
        text = get_message_text(message)
        source_visible, source_html = format_source_parts(chat, message_id)

        if not is_downloadable_media(message):
            if text:
                await send_html_messages(iter_text_chunks(text, source_visible, source_html))
                return
            if getattr(message, "media", None):
                logging.warning(f"Неподдерживаемый тип медиа: {type(message.media)}")
                await send_html_messages(
                    iter_text_chunks("⚠️ Неподдерживаемый тип медиа", source_visible, source_html)
                )
            else:
                await send_html_messages(
                    iter_text_chunks("📎 Медиа без текста", source_visible, source_html)
                )
            return

        buffer = None
        try:
            try:
                buffer = await download_to_buffer(message)
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
                await send_remainder(
                    remainder,
                    source_visible,
                    source_html,
                    reply_to_message_id=getattr(sent, "message_id", None),
                )
            except Exception as send_error:
                logging.error(
                    f"Ошибка при отправке медиа сообщения {message_id}: {send_error}",
                    exc_info=True,
                )
                fallback = f"{text}\n\n⚠️ Не удалось отправить медиа" if text else "⚠️ Не удалось отправить медиа"
                await send_html_messages(iter_text_chunks(fallback, source_visible, source_html))
        finally:
            if buffer is not None:
                buffer.close()

    except Exception as e:
        logging.error(f"Ошибка при копировании сообщения: {str(e)}", exc_info=True)

async def copy_album_to_channel(event):
    """Скачивает все файлы альбома и публикует их одним media group."""
    messages = list(event.messages or [])
    if not messages:
        return

    chat = event.chat
    text = get_album_text(event)
    source_visible, source_html = format_source_parts(chat, messages[0].id)
    logging.info(f"Копирование альбома из {len(messages)} сообщений ({chat.id})")

    items = []
    try:
        for message in messages:
            if not is_downloadable_media(message):
                continue
            try:
                items.append((message, await download_to_buffer(message)))
            except Exception as download_error:
                logging.error(
                    f"Ошибка при загрузке медиа альбома {message.id}: {download_error}",
                    exc_info=True,
                )

        if not items:
            fallback = text if text else "⚠️ Не удалось загрузить медиа"
            await send_html_messages(iter_text_chunks(fallback, source_visible, source_html))
            return

        caption, remainder = build_media_caption(text, source_visible, source_html)
        if remainder:
            logging.info("Подпись альбома превышает лимит, хвост текста отправим отдельно")

        try:
            sent = await send_grouped_media(items, caption)
            reply_to = sent[0].message_id if sent else None
            await send_remainder(remainder, source_visible, source_html, reply_to_message_id=reply_to)
        except Exception as send_error:
            logging.error(f"Ошибка при отправке альбома: {send_error}", exc_info=True)
            fallback = f"{text}\n\n⚠️ Не удалось отправить медиа" if text else "⚠️ Не удалось отправить медиа"
            await send_html_messages(iter_text_chunks(fallback, source_visible, source_html))
    finally:
        for _, buffer in items:
            buffer.close()
            
def chat_log_name(chat):
    if chat.username:
        return f"@{chat.username}"
    if hasattr(chat, "title") and chat.title:
        return chat.title
    return f"id{chat.id}"

async def is_relevant_text(text):
    if not text:
        return True
    return await asyncio.to_thread(check_topic_relevance, text)

# Обработчик новых сообщений из каналов
async def handle_new_message(event):
    try:
        message = event.message
        if getattr(message, "grouped_id", None):
            return

        chat = event.chat
        message_id = message.id
        logging.info(f"Новое сообщение {message_id} из {chat_log_name(chat)}")

        message_text = get_message_text(message)
        if await is_relevant_text(message_text):
            await send_message_to_channel(event)
        else:
            logging.info(f"Сообщение {message_id} не релевантно, пропускаем")

    except Exception as e:
        logging.error(f"Ошибка обработки сообщения: {str(e)}", exc_info=True)

async def handle_album(event):
    try:
        messages = list(event.messages or [])
        chat = event.chat
        logging.info(f"Новый альбом из {len(messages)} сообщений в {chat_log_name(chat)}")

        if await is_relevant_text(get_album_text(event)):
            await copy_album_to_channel(event)
        else:
            logging.info("Альбом не релевантен, пропускаем")
    except Exception as e:
        logging.error(f"Ошибка обработки альбома: {str(e)}", exc_info=True)

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
        
        # Альбом приходит и как NewMessage на каждый файл, и как events.Album.
        # grouped_id пропускаем в NewMessage, иначе уйдёт только один файл без подписи.
        @client.on(events.Album(chats=valid_channels))
        async def album_handler(event):
            await handle_album(event)

        @client.on(events.NewMessage(chats=valid_channels))
        async def message_handler(event):
            await handle_new_message(event)
        
        logging.info("Обработчики сообщений и альбомов успешно зарегистрированы")
        
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