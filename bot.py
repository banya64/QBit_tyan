import asyncio
import logging
import os
import re
import time
import uuid
from urllib.parse import parse_qs, unquote_plus, urlparse

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


# ---------------------------------------------------------------- config ----
def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise SystemExit(f"Не задана обязательная переменная окружения: {name}")
    return value or ""


BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", required=True)
ALLOWED_USER_IDS = {
    int(x) for x in re.split(r"[,\s]+", env("ALLOWED_USER_IDS", required=True)) if x
}

QBIT_URL = env("QBIT_URL", required=True).rstrip("/")
QBIT_USERNAME = env("QBIT_USERNAME", "admin")
QBIT_PASSWORD = env("QBIT_PASSWORD", "")
QBIT_VERIFY_SSL = env("QBIT_VERIFY_SSL", "true").lower() in ("1", "true", "yes")
QBIT_CATEGORY_PREFIX = env("QBIT_CATEGORY_PREFIX", "")  # пусто = без категорий
QBIT_START_PAUSED = env("QBIT_START_PAUSED", "false").lower() in ("1", "true", "yes")

# Пути указываются так, как их видит qBittorrent (внутри его контейнера/хоста)
FOLDERS: dict[str, tuple[str, str]] = {
    "series": ("📺 Сериалы", env("PATH_SERIES", required=True)),
    "movies": ("🎬 Фильмы", env("PATH_MOVIES", required=True)),
    "music": ("🎵 Музыка", env("PATH_MUSIC", required=True)),
}

PENDING_TTL = int(env("PENDING_TTL_SECONDS", "3600"))
LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("magnet-bot")

MAGNET_RE = re.compile(r"magnet:\?[^\s]+", re.IGNORECASE)


# ----------------------------------------------------------------- qbit -----
class QbitError(Exception):
    pass


class Qbit:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=QBIT_URL,
            verify=QBIT_VERIFY_SSL,
            timeout=15,
            headers={"Referer": QBIT_URL, "Origin": QBIT_URL},
        )
        self._lock = asyncio.Lock()

    async def login(self) -> None:
        r = await self.client.post(
            "/api/v2/auth/login",
            data={"username": QBIT_USERNAME, "password": QBIT_PASSWORD},
        )
        if r.status_code != 200 or r.text.strip() != "Ok.":
            raise QbitError(f"Не удалось войти в qBittorrent: {r.status_code} {r.text!r}")
        log.info("Авторизация в qBittorrent успешна")

    async def add(self, magnets: list[str], savepath: str, category: str = "") -> None:
        data = {
            "urls": "\n".join(magnets),
            "savepath": savepath,
            "autoTMM": "false",  # иначе qbit проигнорирует savepath
            "paused": "true" if QBIT_START_PAUSED else "false",
        }
        if category:
            data["category"] = category

        for attempt in (1, 2):
            try:
                r = await self.client.post("/api/v2/torrents/add", data=data)
            except httpx.HTTPError as e:
                raise QbitError(f"qBittorrent недоступен: {e}") from e

            if r.status_code == 403 and attempt == 1:
                async with self._lock:
                    await self.login()
                continue
            if r.status_code == 200 and r.text.strip() == "Ok.":
                return
            raise QbitError(f"qBittorrent ответил: {r.status_code} {r.text!r}")

    async def close(self) -> None:
        await self.client.aclose()


qbit = Qbit()


# ------------------------------------------------------------------ bot -----
dp = Dispatcher()

# token -> {"user": int, "magnets": [...], "ts": float}
pending: dict[str, dict] = {}


def cleanup_pending() -> None:
    now = time.time()
    for token in [t for t, v in pending.items() if now - v["ts"] > PENDING_TTL]:
        pending.pop(token, None)


def magnet_title(magnet: str) -> str:
    qs = parse_qs(urlparse(magnet).query)
    if "dn" in qs:
        return unquote_plus(qs["dn"][0])
    xt = qs.get("xt", [""])[0]
    return xt.split(":")[-1][:12] + "…" if xt else "без названия"


def is_allowed(user_id: int | None) -> bool:
    return user_id in ALLOWED_USER_IDS


def keyboard(token: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"dl:{token}:{key}")]
        for key, (title, _) in FOLDERS.items()
    ]
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data=f"dl:{token}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    if not is_allowed(message.from_user.id if message.from_user else None):
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer(
        "Пришли magnet-ссылку (можно несколько в одном сообщении), "
        "затем выбери папку: сериалы, фильмы или музыка."
    )


@dp.message(F.text)
async def on_text(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_allowed(user_id):
        log.warning("Отказано пользователю %s", user_id)
        await message.answer("⛔ Доступ запрещён.")
        return

    magnets = MAGNET_RE.findall(message.text or "")
    if not magnets:
        await message.answer("Не вижу magnet-ссылки 🤔 Она должна начинаться с `magnet:?`", parse_mode="Markdown")
        return

    cleanup_pending()
    token = uuid.uuid4().hex[:10]
    pending[token] = {"user": user_id, "magnets": magnets, "ts": time.time()}

    names = "\n".join(f"• {magnet_title(m)}" for m in magnets[:10])
    more = f"\n…и ещё {len(magnets) - 10}" if len(magnets) > 10 else ""
    await message.answer(
        f"Нашёл ссылок: {len(magnets)}\n{names}{more}\n\nКуда скачать?",
        reply_markup=keyboard(token),
    )


@dp.callback_query(F.data.startswith("dl:"))
async def on_choice(cb: CallbackQuery) -> None:
    if not is_allowed(cb.from_user.id):
        await cb.answer("⛔ Доступ запрещён", show_alert=True)
        return

    _, token, key = cb.data.split(":", 2)
    item = pending.get(token)
    if not item or item["user"] != cb.from_user.id:
        await cb.answer("Запрос устарел, пришли ссылку заново", show_alert=True)
        if cb.message:
            await cb.message.edit_reply_markup(reply_markup=None)
        return

    if key == "cancel":
        pending.pop(token, None)
        await cb.message.edit_text("Отменено.")
        await cb.answer()
        return

    if key not in FOLDERS:
        await cb.answer("Неизвестная папка", show_alert=True)
        return

    title, path = FOLDERS[key]
    category = f"{QBIT_CATEGORY_PREFIX}{key}" if QBIT_CATEGORY_PREFIX else ""
    try:
        await qbit.add(item["magnets"], path, category)
    except QbitError as e:
        log.error("Ошибка добавления: %s", e)
        await cb.answer("Ошибка, подробности в сообщении", show_alert=False)
        await cb.message.edit_text(f"❌ Не получилось добавить:\n{e}", reply_markup=keyboard(token))
        return

    pending.pop(token, None)
    log.info("Добавлено %d торрент(ов) в %s (user %s)", len(item["magnets"]), path, cb.from_user.id)
    await cb.message.edit_text(
        f"✅ Добавлено торрентов: {len(item['magnets'])}\nПапка: {title}\n`{path}`",
        parse_mode="Markdown",
    )
    await cb.answer()


async def main() -> None:
    bot = Bot(BOT_TOKEN)
    try:
        await qbit.login()
    except Exception as e:  # qbit может подняться позже бота — не падаем, залогинимся при первом запросе
        log.warning("Первичный логин не удался (%s), повторю при добавлении", e)
    try:
        await dp.start_polling(bot)
    finally:
        await qbit.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
