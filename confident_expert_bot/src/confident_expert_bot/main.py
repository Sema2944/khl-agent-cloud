from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from io import BytesIO

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from confident_expert_bot.gpt_client import GptClient
from confident_expert_bot.prompts import build_prompt_for_check, build_prompt_for_outfit
from confident_expert_bot.s3_client import S3Client
from confident_expert_bot.settings import settings
from confident_expert_bot.storage import Storage

logging.basicConfig(level=logging.INFO)

router = Router()


@dataclass(frozen=True)
class States:
    awaiting_context: str = "awaiting_context"
    awaiting_photos: str = "awaiting_photos"
    awaiting_build_confirm: str = "awaiting_build_confirm"
    awaiting_check_outfit: str = "awaiting_check_outfit"
    awaiting_photo_description: str = "awaiting_photo_description"


@dataclass(frozen=True)
class Callbacks:
    mode_build: str = "mode_build"
    mode_check: str = "mode_check"
    build_confirm: str = "build_confirm"
    build_again: str = "build_again"
    check_use_last: str = "check_use_last"
    done: str = "done"


CONTEXT_OPTIONS = {
    "context_street": "Улица",
    "context_meeting": "Встреча",
    "context_shoot": "Съёмка",
    "context_talk": "Выступление",
}


def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧥 Собрать образ", callback_data=Callbacks.mode_build)],
            [InlineKeyboardButton(text="✅ Проверить уверенность", callback_data=Callbacks.mode_check)],
        ]
    )


def context_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=key)]
            for key, label in CONTEXT_OPTIONS.items()
        ]
    )


def build_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Собрать образ", callback_data=Callbacks.build_confirm)]]
    )


def after_build_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Проверить уверенность", callback_data=Callbacks.mode_check)],
            [InlineKeyboardButton(text="🔄 Собрать заново", callback_data=Callbacks.build_again)],
        ]
    )


def check_keyboard(include_last: bool) -> InlineKeyboardMarkup | None:
    if not include_last:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Использовать последний образ", callback_data=Callbacks.check_use_last)]
        ]
    )


def done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🚪 Всё, иду", callback_data=Callbacks.done)]]
    )


async def ensure_allowed(message: Message, storage: Storage) -> bool:
    await storage.ensure_user(message.from_user.id)
    allowed = await storage.is_allowed(message.from_user.id)
    if not allowed:
        await message.answer("Доступ к боту ограничен")
    return allowed


async def ensure_allowed_callback(query: CallbackQuery, storage: Storage) -> bool:
    await storage.ensure_user(query.from_user.id)
    allowed = await storage.is_allowed(query.from_user.id)
    if not allowed:
        await query.message.answer("Доступ к боту ограничен")
    return allowed


@router.message(Command("start"))
async def start_handler(message: Message, storage: Storage) -> None:
    if not await ensure_allowed(message, storage):
        return
    await storage.clear_session(message.from_user.id)
    await message.answer(
        "Я помогаю собрать образ или проверить уверенность перед выходом.",
        reply_markup=mode_keyboard(),
    )


@router.message(Command("add_user"))
async def add_user_handler(message: Message, storage: Storage) -> None:
    if message.from_user.id not in settings.admin_ids:
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /add_user <telegram_id>")
        return
    await storage.add_user(int(parts[1]))
    await message.answer("Пользователь добавлен")


@router.message(Command("remove_user"))
async def remove_user_handler(message: Message, storage: Storage) -> None:
    if message.from_user.id not in settings.admin_ids:
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /remove_user <telegram_id>")
        return
    await storage.remove_user(int(parts[1]))
    await message.answer("Пользователь удалён")


@router.message(Command("list_users"))
async def list_users_handler(message: Message, storage: Storage) -> None:
    if message.from_user.id not in settings.admin_ids:
        return
    users = await storage.list_users()
    if not users:
        await message.answer("Список пуст")
        return
    await message.answer("\n".join(str(user_id) for user_id in users))


@router.message(Command("build"))
async def build_command_handler(message: Message, storage: Storage) -> None:
    if not await ensure_allowed(message, storage):
        return
    await storage.update_session(message.from_user.id, States.awaiting_context, {})
    await message.answer("Куда ты идёшь?", reply_markup=context_keyboard())


@router.message(Command("check"))
async def check_command_handler(message: Message, storage: Storage) -> None:
    if not await ensure_allowed(message, storage):
        return
    session = await storage.get_session(message.from_user.id)
    await storage.update_session(message.from_user.id, States.awaiting_check_outfit, session.payload)
    await message.answer(
        "Опиши образ текстом или выбери последний.",
        reply_markup=check_keyboard(include_last=bool(session.last_outfit)),
    )


@router.message(Command("skip_photos"))
async def skip_photos_handler(message: Message, storage: Storage) -> None:
    if not await ensure_allowed(message, storage):
        return
    session = await storage.get_session(message.from_user.id)
    if session.state != States.awaiting_photos:
        return
    await storage.update_session(message.from_user.id, States.awaiting_build_confirm, session.payload)
    await message.answer("Фото пропущены.", reply_markup=build_confirm_keyboard())


@router.callback_query(F.data == Callbacks.mode_build)
async def mode_build_handler(query: CallbackQuery, storage: Storage) -> None:
    if not await ensure_allowed_callback(query, storage):
        return
    await storage.update_session(query.from_user.id, States.awaiting_context, {})
    await query.message.answer("Куда ты идёшь?", reply_markup=context_keyboard())
    await query.answer()


@router.callback_query(F.data == Callbacks.mode_check)
async def mode_check_handler(query: CallbackQuery, storage: Storage) -> None:
    if not await ensure_allowed_callback(query, storage):
        return
    session = await storage.get_session(query.from_user.id)
    await storage.update_session(query.from_user.id, States.awaiting_check_outfit, session.payload)
    await query.message.answer(
        "Опиши образ текстом или выбери последний.",
        reply_markup=check_keyboard(include_last=bool(session.last_outfit)),
    )
    await query.answer()


@router.callback_query(F.data == Callbacks.build_again)
async def build_again_handler(query: CallbackQuery, storage: Storage) -> None:
    if not await ensure_allowed_callback(query, storage):
        return
    await storage.update_session(query.from_user.id, States.awaiting_context, {})
    await query.message.answer("Куда ты идёшь?", reply_markup=context_keyboard())
    await query.answer()


@router.callback_query(F.data.in_(CONTEXT_OPTIONS.keys()))
async def context_handler(query: CallbackQuery, storage: Storage) -> None:
    if not await ensure_allowed_callback(query, storage):
        return
    context_label = CONTEXT_OPTIONS[query.data]
    await storage.update_session(
        query.from_user.id,
        States.awaiting_photos,
        {"context": context_label},
        context=context_label,
    )
    await query.message.answer(
        "Загрузи 1–6 фото одежды или пропусти командой /skip_photos.",
    )
    await query.answer()


@router.message(F.photo)
async def photo_handler(message: Message, storage: Storage, s3: S3Client) -> None:
    if not await ensure_allowed(message, storage):
        return
    session = await storage.get_session(message.from_user.id)
    if session.state not in {States.awaiting_photos, States.awaiting_build_confirm}:
        return
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    download = await message.bot.download_file(file.file_path)
    content = download.getvalue() if isinstance(download, BytesIO) else download.read()
    uploaded = await s3.upload_bytes(
        content=content,
        content_type="image/jpeg",
        prefix=f"photos/{message.from_user.id}",
    )
    await storage.add_photo(message.from_user.id, photo.file_id, uploaded.key)
    await storage.update_session(message.from_user.id, States.awaiting_build_confirm, session.payload)
    await message.answer("Фото получено.", reply_markup=build_confirm_keyboard())


@router.callback_query(F.data == Callbacks.build_confirm)
async def build_confirm_handler(
    query: CallbackQuery, storage: Storage, gpt: GptClient, s3: S3Client
) -> None:
    if not await ensure_allowed_callback(query, storage):
        return
    session = await storage.get_session(query.from_user.id)
    context = session.payload.get("context")
    photos = await storage.list_recent_photos(query.from_user.id)
    photo_desc = None
    if photos:
        try:
            urls = [await s3.presign_get_url(photo["s3_key"]) for photo in photos if photo["s3_key"]]
            photo_desc = await gpt.describe_photos(urls)
        except Exception:
            await storage.update_session(
                query.from_user.id,
                States.awaiting_photo_description,
                {"context": context},
                context=context,
            )
            await query.message.answer(
                "Опиши одежду текстом (верх / низ / обувь / аксессуары / верхняя одежда / цвета)."
            )
            await query.answer()
            return
    response = await build_outfit_response(
        user_id=query.from_user.id,
        storage=storage,
        gpt=gpt,
        context=context,
        photo_desc=photo_desc,
    )
    await query.message.answer(response, reply_markup=after_build_keyboard())
    await query.answer()


@router.callback_query(F.data == Callbacks.check_use_last)
async def check_use_last_handler(
    query: CallbackQuery, storage: Storage, gpt: GptClient
) -> None:
    if not await ensure_allowed_callback(query, storage):
        return
    session = await storage.get_session(query.from_user.id)
    if not session.last_outfit:
        await query.message.answer("Нет сохранённого образа.")
        await query.answer()
        return
    prompt = build_prompt_for_check(
        outfit_text=session.last_outfit,
        context=session.context,
        verified="",
        bans="",
    )
    response = await gpt.complete(prompt)
    await storage.update_session(query.from_user.id, None, {}, last_outfit=session.last_outfit)
    await query.message.answer(response, reply_markup=done_keyboard())
    await query.answer()


@router.message(F.text)
async def text_handler(message: Message, storage: Storage, gpt: GptClient) -> None:
    if not await ensure_allowed(message, storage):
        return
    session = await storage.get_session(message.from_user.id)
    if session.state == States.awaiting_photo_description:
        response = await build_outfit_response(
            user_id=message.from_user.id,
            storage=storage,
            gpt=gpt,
            context=session.context,
            photo_desc=message.text,
        )
        await message.answer(response, reply_markup=after_build_keyboard())
        return
    if session.state == States.awaiting_check_outfit or session.state is None:
        prompt = build_prompt_for_check(
            outfit_text=message.text,
            context=session.context,
            verified="",
            bans="",
        )
        response = await gpt.complete(prompt)
        await storage.update_session(message.from_user.id, None, {}, last_outfit=message.text)
        await message.answer(response, reply_markup=done_keyboard())
        return
    if session.state == States.awaiting_photos:
        await message.answer("Жду фото одежды или команду /skip_photos.")
        return
    if session.state == States.awaiting_build_confirm:
        await message.answer("Нажми «Собрать образ».", reply_markup=build_confirm_keyboard())
        return


@router.callback_query(F.data == Callbacks.done)
async def done_handler(query: CallbackQuery) -> None:
    await query.answer()


async def build_outfit_response(
    *,
    user_id: int,
    storage: Storage,
    gpt: GptClient,
    context: str | None,
    photo_desc: str | None,
) -> str:
    prompt = build_prompt_for_outfit(
        context=context,
        wardrobe="",
        bans="",
        photo_desc=photo_desc,
    )
    response = await gpt.complete(prompt)
    await storage.update_session(
        user_id,
        None,
        {},
        last_outfit=response,
        context=context,
    )
    return response


async def main() -> None:
    storage = Storage(settings.database_path)
    await storage.init()
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    gpt = GptClient(api_key=settings.openai_api_key)
    s3 = S3Client(
        bucket=settings.s3_bucket,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
        endpoint_url=settings.s3_endpoint_url,
    )
    dp.include_router(router)
    dp["storage"] = storage
    dp["gpt"] = gpt
    dp["s3"] = s3
    await dp.start_polling(bot, storage=storage, gpt=gpt, s3=s3)


if __name__ == "__main__":
    asyncio.run(main())
