import os
import uuid
import asyncio
import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from pydub import AudioSegment
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, APIC, ID3NoHeaderError
from PIL import Image
import io

BOT_TOKEN = "8828176254:AAFyt9AHjCMP38yX-8SSI7i3nEkkNe-2rLA"
ADMIN_USERNAME = "@Salyaf_ru"

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
    batch_processing = State()

user_data: dict[int, dict] = {}

def get_skip_button():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="️ Пропустить", callback_data="skip_cover")]
    ])
    return keyboard

def get_batch_buttons():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Завершить и конвертировать", callback_data="batch_finish")]
    ])
    return keyboard

def get_start_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Связаться с админом", url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}")]
    ])
    return keyboard

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎵 <b>Привет! Я конвертер голосовых в MP3.</b>\n\n"
        "📝 <b>Как использовать:</b>\n"
        "1️⃣ Отправь <b>голосовое сообщение</b>\n"
        "2️⃣ Введи название трека\n"
        "3️⃣ Введи исполнителя\n"
        "4️ Отправь обложку или пропусти\n\n"
        "💡 <b>Команды:</b>\n"
        "/help - Помощь\n"
        "/cancel - Отменить конвертацию\n"
        "/batch - Пакетная обработка",
        parse_mode="HTML",
        reply_markup=get_start_keyboard()
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Справка по боту</b>\n\n"
        "🎯 <b>Основные функции:</b>\n"
        "• Конвертация голосовых в MP3\n"
        "• Добавление метаданных (название, исполнитель)\n"
        "• Добавление обложки альбома\n"
        "• Пакетная обработка нескольких файлов\n"
        "• Объединение голосовых в один трек\n\n"
        "📝 <b>Команды:</b>\n"
        "/start - Начать работу\n"
        "/help - Эта справка\n"
        "/cancel - Отменить текущую операцию\n"
        "/batch - Пакетная обработка\n\n"
        " <b>Советы:</b>\n"
        "• Можно отправить фото для обложки\n"
        "• Или пропустить этот шаг кнопкой\n"
        "• Для связи с админом нажми /start"
    )

@router.message(Command("batch"))
async def cmd_batch(message: Message, state: FSMContext):
    chat_id = message.chat.id
    user_data[chat_id] = {
        "voice_files": [],
        "session_id": str(uuid.uuid4())[:8],
        "merge": False
    }
    await state.set_state(ConvertStates.batch_processing)
    await message.answer(
        "📦 <b>Пакетная обработка активирована!</b>\n\n"
        "Отправляй <b>несколько голосовых сообщений</b>.\n"
        "Когда закончишь — нажми кнопку ниже.\n\n"
        " Все файлы будут конвертированы и отправлены по отдельности.",
        parse_mode="HTML",
        reply_markup=get_batch_buttons()
    )

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    chat_id = message.chat.id
    if chat_id in user_data:
        cleanup_files(user_data[chat_id])
        del user_data[chat_id]
    await state.clear()
    await message.answer("🚫 Отменено. Отправь голосовое чтобы начать заново.")

@router.callback_query(F.data == "skip_cover")
async def callback_skip_cover(callback_query, state: FSMContext):
    chat_id = callback_query.message.chat.id
    if chat_id not in user_data:
        await callback_query.answer("❌ Сессия не найдена", show_alert=True)
        return
    
    user_data[chat_id]["cover_path"] = None
    await process_final_mp3(callback_query.message, state)
    await callback_query.answer()

@router.callback_query(F.data == "batch_finish")
async def callback_batch_finish(callback_query, state: FSMContext):
    chat_id = callback_query.message.chat.id
    if chat_id not in user_data or not user_data[chat_id].get("voice_files"):
        await callback_query.answer("❌ Нет файлов для обработки", show_alert=True)
        return
    
    await callback_query.message.answer(f"⏳ Обрабатываю {len(user_data[chat_id]['voice_files'])} файлов...")
    
    for idx, voice_data in enumerate(user_data[chat_id]["voice_files"], 1):
        await callback_query.message.answer(f" Обработка {idx}/{len(user_data[chat_id]['voice_files'])}...")
        try:
            await process_single_voice(callback_query.message, voice_data, state)
        except Exception as e:
            logger.error(f"Ошибка обработки файла {idx}: {e}")
            await callback_query.message.answer(f"❌ Ошибка в файле {idx}: {e}")
    
    cleanup_files(user_data[chat_id])
    del user_data[chat_id]
    await state.clear()
    await callback_query.message.answer("✅ Все файлы обработаны!")
    await callback_query.answer()

async def process_single_voice(message: Message, voice_data: dict, state: FSMContext):
    chat_id = message.chat.id
    ogg_path = voice_data["ogg_path"]
    session_id = voice_data.get("session_id", str(uuid.uuid4())[:8])
    mp3_path = os.path.join(TEMP_DIR, f"{session_id}_output.mp3")
    
    audio = AudioSegment.from_file(ogg_path, format="ogg")
    audio.export(mp3_path, format="mp3", bitrate="192k")
    
    user_data[chat_id].update({
        "mp3_path": mp3_path,
        "title": voice_data.get("title", "Unknown"),
        "artist": voice_data.get("artist", "Unknown"),
        "cover_path": None,
        "session_id": session_id
    })
    
    await process_final_mp3(message, state, is_batch=True)

async def process_final_mp3(message: Message, state: FSMContext, is_batch=False):
    chat_id = message.chat.id
    data = user_data[chat_id]
    mp3_path = data["mp3_path"]
    title = data["title"]
    artist = data["artist"]
    cover_path = data["cover_path"]
    
    await message.answer("⏳ Записываю теги...")
    add_id3_tags(mp3_path, title, artist, cover_path)
    
    safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:50]
    safe_artist = "".join(c for c in artist if c.isalnum() or c in " -_").strip()[:50]
    file_name = f"{safe_artist} - {safe_title}.mp3"
    
    final_path = os.path.join(TEMP_DIR, f"{data['session_id']}_final.mp3")
    os.rename(mp3_path, final_path)
    data["mp3_path"] = final_path
    
    audio_file = FSInputFile(final_path, filename=file_name)
    await message.answer_audio(
        audio=audio_file,
        title=title,
        performer=artist,
    )
    
    if not is_batch:
        cleanup_files(data)
        del user_data[chat_id]
        await state.clear()

@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext):
    current_state = await state.get_state()
    
    if current_state == ConvertStates.batch_processing:
        await handle_batch_voice(message, state)
        return
    
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
        
        await message.answer("🔄 Конвертирую в MP3...")
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

async def handle_batch_voice(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await message.answer("➕ Добавляю в очередь...")
    
    try:
        voice = message.voice
        file_info = await bot.get_file(voice.file_id)
        file_path = file_info.file_path
        
        session_id = str(uuid.uuid4())[:8]
        ogg_path = os.path.join(TEMP_DIR, f"{session_id}_input.ogg")
        
        await bot.download_file(file_path, ogg_path)
        
        user_data[chat_id]["voice_files"].append({
            "ogg_path": ogg_path,
            "session_id": session_id,
            "title": "Unknown",
            "artist": "Unknown"
        })
        
        count = len(user_data[chat_id]["voice_files"])
        await message.answer(
            f"✅ Добавлено файлов: {count}\n\n"
            "Отправляй ещё или нажми кнопку ниже.",
            reply_markup=get_batch_buttons()
        )
        
    except Exception as e:
        logger.error(f"Ошибка в пакетной обработке: {e}")
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
    await message.answer(f"🎵 Название: <b>{title}</b>\n\n Введи <b>исполнителя</b>:", parse_mode="HTML")

@router.message(ConvertStates.waiting_for_artist, F.text)
async def handle_artist(message: Message, state: FSMContext):
    chat_id = message.chat.id
    artist = message.text.strip()
    
    if not artist or len(artist) > 200:
        await message.answer("⚠️ Введи имя исполнителя (1–200 символов):")
        return
    
    user_data[chat_id]["artist"] = artist
    await state.set_state(ConvertStates.waiting_for_cover)
    await message.answer(
        f"🎤 Исполнитель: <b>{artist}</b>\n\n"
        "🖼️ Отправь <b>фото</b> для обложки или нажми кнопку ниже:",
        parse_mode="HTML",
        reply_markup=get_skip_button()
    )

@router.message(ConvertStates.waiting_for_cover, F.photo)
async def handle_cover_photo(message: Message, state: FSMContext):
    chat_id = message.chat.id
    
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    cover_path = os.path.join(TEMP_DIR, f"{user_data[chat_id]['session_id']}_cover.jpg")
    await bot.download_file(file_info.file_path, cover_path)
    user_data[chat_id]["cover_path"] = cover_path
    
    await process_final_mp3(message, state)

def add_id3_tags(mp3_path: str, title: str, artist: str, cover_path: str | None):
    try:
        audio = ID3(mp3_path)
    except ID3NoHeaderError:
        audio = ID3()
    
    audio.clear()
    audio["TIT2"] = TIT2(encoding=3, text=title)
    audio["TPE1"] = TPE1(encoding=3, text=artist)
    
    if cover_path and os.path.exists(cover_path):
        try:
            with Image.open(cover_path) as img:
                # Конвертируем в RGB если нужно
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                
                # Для Android: уменьшаем до 300x300 (лучше совместимость)
                img = img.resize((300, 300), Image.Resampling.LANCZOS)
                
                # Сохраняем в bytes
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='JPEG', quality=90, optimize=True)
                img_byte_arr.seek(0)
                cover_data = img_byte_arr.read()
            
            # Добавляем обложку с параметрами для Android
            audio["APIC"] = APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,  # Front cover
                desc="",  # Пустое описание для совместимости
                data=cover_data
            )
            logger.info(f"✅ Обложка добавлена (300x300, {len(cover_data)} байт)")
        except Exception as e:
            logger.error(f"❌ Ошибка обработки обложки: {e}")
    
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
    
    if "voice_files" in data:
        for voice_data in data["voice_files"]:
            if "ogg_path" in voice_data and os.path.exists(voice_data["ogg_path"]):
                try:
                    os.remove(voice_data["ogg_path"])
                except OSError:
                    pass

async def main():
    dp.include_router(router)
    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
