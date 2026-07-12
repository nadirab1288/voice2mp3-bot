import os
import uuid
import asyncio
import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from pydub import AudioSegment
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, APIC, ID3NoHeaderError

BOT_TOKEN = "8828176254:AAFyt9AHjCMP38yX-8SSI7i3nEkkNe-2rLA"

TEMP_DIR = "temp"
os.makedirs(TEMP_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

class ConvertStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_artist = State()
    waiting_for_cover = State()

user_data: dict[int, dict] = {}

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎵 <b>Привет! Я конвертер голосовых в MP3.</b>\n\n"
        "Просто отправь мне <b>голосовое сообщение</b>!",
        parse_mode="HTML"
    )

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    chat_id = message.chat.id
    if chat_id in user_data:
        cleanup_files(user_data[chat_id])
        del user_data[chat_id]
    await state.clear()
    await message.answer("🚫 Отменено.")

@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await message.answer("⏳ Скачиваю...")

    try:
        voice = message.voice
        file_info = await bot.get_file(voice.file_id)
        file_path = file_info.file_path

        session_id = str(uuid.uuid4())[:8]
        ogg_path = os.path.join(TEMP_DIR, f"{session_id}_input.ogg")
        mp3_path = os.path.join(TEMP_DIR, f"{session_id}_output.mp3")

        await bot.download_file(file_path, ogg_path)

        await message.answer(" Конвертирую в MP3...")
        audio = AudioSegment.from_file(ogg_path, format="ogg")
        audio.export(mp3_path, format="mp3", bitrate="192k")

        user_data[chat_id] = {
            "session_id": session_id,
            "ogg_path": ogg_path,
            "mp3_path": mp3_path,
            "title": None,
            "artist": None,
            "cover_path": None,
        }

        await state.set_state(ConvertStates.waiting_for_title)
        await message.answer("✅ Готово! Введи <b>название трека</b>:", parse_mode="HTML")

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await message.answer(f"❌ Ошибка: {e}")

@router.message(ConvertStates.waiting_for_title, F.text)
async def handle_title(message: Message, state: FSMContext):
    chat_id = message.chat.id
    title = message.text.strip()

    if not title or len(title) > 200:
        await message.answer("⚠️ Введи название (1–200 символов):")
        return

    user_data[chat_id]["title"] = title
    await state.set_state(ConvertStates.waiting_for_artist)
    await message.answer(f"🎵 Название: <b>{title}</b>\n\n🎤 Введи <b>исполнителя</b>:", parse_mode="HTML")

@router.message(ConvertStates.waiting_for_artist, F.text)
async def handle_artist(message: Message, state: FSMContext):
    chat_id = message.chat.id
    artist = message.text.strip()

    if not artist or len(artist) > 200:
        await message.answer("⚠️ Введи имя исполнителя (1–200 символов):")
        return

    user_data[chat_id]["artist"] = artist
    await state.set_state(ConvertStates.waiting_for_cover)
    await message.answer(f"🎤 Исполнитель: <b>{artist}</b>\n\n🖼️ Отправь <b>фото</b> для обложки или напиши <code>skip</code>:", parse_mode="HTML")

@router.message(ConvertStates.waiting_for_cover)
async def handle_cover(message: Message, state: FSMContext):
    chat_id = message.chat.id

    if message.text and message.text.strip().lower() in ("skip", "пропустить", "-"):
        user_data[chat_id]["cover_path"] = None
    elif message.photo:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        cover_path = os.path.join(TEMP_DIR, f"{user_data[chat_id]['session_id']}_cover.jpg")
        await bot.download_file(file_info.file_path, cover_path)
        user_data[chat_id]["cover_path"] = cover_path
    else:
        await message.answer("🖼️ Отправь <b>фото</b> или напиши <code>skip</code>:", parse_mode="HTML")
        return

    await message.answer("⏳ Записываю теги...")

    try:
        data = user_data[chat_id]
        mp3_path = data["mp3_path"]
        title = data["title"]
        artist = data["artist"]
        cover_path = data["cover_path"]

        add_id3_tags(mp3_path, title, artist, cover_path)

        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:50]
        safe_artist = "".join(c for c in artist if c.isalnum() or c in " -_").strip()[:50]
        file_name = f"{safe_artist} - {safe_title}.mp3"

        final_path = os.path.join(TEMP_DIR, f"{data['session_id']}_final.mp3")
        os.rename(mp3_path, final_path)
        data["mp3_path"] = final_path

        # Отправляем аудио БЕЗ подписи (чтобы не дублировалось)
        audio_file = FSInputFile(final_path, filename=file_name)
        await message.answer_audio(
            audio=audio_file,
            title=title,
            performer=artist,
            # caption убран - метаданные уже в файле
        )

        cleanup_files(data)
        del user_data[chat_id]
        await state.clear()

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await message.answer(f"❌ Ошибка: {e}")
        if chat_id in user_data:
            cleanup_files(user_data[chat_id])
            del user_data[chat_id]
        await state.clear()

def add_id3_tags(mp3_path: str, title: str, artist: str, cover_path: str | None):
    try:
        audio = ID3(mp3_path)
    except ID3NoHeaderError:
        audio = ID3()

    # Очищаем старые теги
    audio.clear()
    
    # Добавляем новые теги
    audio["TIT2"] = TIT2(encoding=3, text=title)
    audio["TPE1"] = TPE1(encoding=3, text=artist)
    
    # Добавляем обложку (если есть)
    if cover_path and os.path.exists(cover_path):
        with open(cover_path, "rb") as f:
            cover_data = f.read()
        audio["APIC"] = APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,  # Front cover
            desc="Cover",
            data=cover_data
        )
    
    audio.save(mp3_path, v2_version=3)
    logger.info(f"✅ Теги записаны: {title} — {artist}")

def cleanup_files(data: dict):
    for key in ("ogg_path", "mp3_path", "cover_path"):
        path = data.get(key)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

async def main():
    dp.include_router(router)
    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
