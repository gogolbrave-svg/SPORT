import asyncio
import datetime
import logging
import os
import random
import re
import sqlite3
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    FSInputFile,
    InputMediaPhoto,
)
from aiohttp import ClientSession, web
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN")
ADMIN_ID_env = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_env) if ADMIN_ID_env and ADMIN_ID_env.isdigit() else 0
MANAGER_ID_env = os.getenv("MANAGER_ID")
MANAGER_ID = int(MANAGER_ID_env) if MANAGER_ID_env and MANAGER_ID_env.isdigit() else 0

if not BOT_TOKEN:
  raise ValueError("❌ BOT_TOKEN не найден!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# === ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ SQLite ===
def init_db():
  with sqlite3.connect("bot_database.db") as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
          CREATE TABLE IF NOT EXISTS orders (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER,
              service_name TEXT,
              total_price REAL,
              status TEXT,
              created_at TEXT,
              target_info TEXT
          )
      """
    )
    cursor.execute(
        """
          CREATE TABLE IF NOT EXISTS banned_users (
              user_id INTEGER PRIMARY KEY
          )
      """
    )
    conn.commit()


def add_order_to_db(
    user_id: int, service_name: str, total_price: float, status: str, target_info: str
):
  with sqlite3.connect("bot_database.db") as conn:
    cursor = conn.cursor()
    date_str = datetime.datetime.now().strftime("%d %B")
    cursor.execute(
        """
          INSERT INTO orders (user_id, service_name, total_price, status, created_at, target_info)
          VALUES (?, ?, ?, ?, ?, ?)
      """,
        (user_id, service_name, total_price, status, date_str, target_info),
    )
    conn.commit()
    return cursor.lastrowid


def get_user_orders_from_db(user_id: int):
  with sqlite3.connect("bot_database.db") as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
          SELECT id, service_name, total_price, status, created_at, target_info 
          FROM orders WHERE user_id = ? ORDER BY id DESC
      """,
        (user_id,),
    )
    rows = cursor.fetchall()
    orders = []
    for row in rows:
      orders.append(
          {
              "id": row[0],
              "service_name": row[1],
              "total_price": row[2],
              "status": row[3],
              "created_at": row[4],
              "target_info": row[5],
          }
      )
    return orders


def is_user_banned(user_id: int) -> bool:
  with sqlite3.connect("bot_database.db") as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return row is not None


def ban_user_in_db(user_id: int):
  with sqlite3.connect("bot_database.db") as conn:
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (user_id,))
    conn.commit()


class OrderForm(StatesGroup):
  service = State()
  target_username = State()
  special_link = State()
  receipt = State()


class SupportForm(StatesGroup):
  reason = State()
  photo = State()


# Словари и базы в памяти для отслеживания активности и реквизитов
KNOWN_USERS = {}
USER_MAPPING = {}         # Кэш для связи username <-> user_id для команды /ban
ABANDONED_CHECKOUTS = {}
CRYPTO_WALLETS = []       # Список криптокошельков: {"address", "asset", "network", "frozen_until"}
CRYPTO_POINTERS = {}      # Индексы для циклического чередования кошельков по сети/монете
BANNER_CACHE = {}         # Кэш file_id баннеров для мгновенной загрузки
PENDING_BILLS = {}        # Выставленные менеджером счета по user_id

router = Router()


def is_manager_authorized(user_id: int) -> bool:
  allowed_ids = {uid for uid in (MANAGER_ID, ADMIN_ID) if uid}
  if not allowed_ids:
    return True
  return user_id in allowed_ids


def _format_price(amount: float | int) -> str:
  if isinstance(amount, float) and amount.is_integer():
    return str(int(amount))
  return str(amount)


def _build_catalog_keyboard() -> InlineKeyboardMarkup:
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔍 Доксинг — 20$", callback_data="srv_Доксинг_20", style="primary")],
          [InlineKeyboardButton(text="💣 Снос Telegram аккаунта — 40$", callback_data="srv_Снос_Telegram_аккаунта_40", style="danger")],
          [InlineKeyboardButton(text="⚡️ DDos-атака сайта — 60$", callback_data="srv_DDos-атака_сайта_60", style="danger")],
          [InlineKeyboardButton(text="📸 Взлом Instagram — 80$", callback_data="srv_Взлом_Instagram_80", style="primary")],
          [InlineKeyboardButton(text="💬 Взлом Telegram аккаунта — 100$", callback_data="srv_Взлом_Telegram_аккаунта_100", style="danger")],
          [InlineKeyboardButton(text="💣 Снос TG канала или бота — 200$", callback_data="srv_Снос_TG_канала_или_бота_200", style="primary")],
          [InlineKeyboardButton(text="🌐 Снос сайта — 300$", callback_data="srv_Снос_сайта_300", style="danger")],
          [InlineKeyboardButton(text="🛠 Особый заказ — Рассчитать стоимость", callback_data="srv_special_order", style="primary")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_home", style="danger")],
      ]
  )


async def _get_payment_context(user_id: int, state: FSMContext) -> dict:
  data = await state.get_data()
  bill = PENDING_BILLS.get(user_id, {})
  total_price = data.get("total_price")
  if total_price is None:
    total_price = bill.get("total_price", 10)
  service_name = data.get("service_name") or bill.get("service_name") or "Особый заказ"
  target_username = data.get("target_username") or bill.get("target_username") or "По счету менеджера"
  return {
      "total_price": total_price,
      "service_name": service_name,
      "target_username": target_username,
  }


def _build_payment_keyboard() -> InlineKeyboardMarkup:
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🤖 Оплата через CryptoBot", callback_data="pay_cryptobot", style="primary")],
          [InlineKeyboardButton(text="🪙 Реквизиты (Крипта)", callback_data="crypto_expert", style="danger")],
          [InlineKeyboardButton(text="💬 Нужна помощь", callback_data="payment_help_manager", style="primary")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog_services", style="danger")],
      ]
  )


# === MIDDLEWARE ДЛЯ ПРОВЕРКИ БАНА ===
@router.message.middleware()
async def ban_middleware(handler, event, data):
  if event.from_user and is_user_banned(event.from_user.id):
    return
  return await handler(event, data)


@router.callback_query.middleware()
async def ban_callback_middleware(handler, event, data):
  if event.from_user and is_user_banned(event.from_user.id):
    await event.answer("❌ Вы заблокированы в этом боте.", show_alert=True)
    return
  return await handler(event, data)


def track_user(user):
  if user:
    USER_MAPPING[user.id] = user.username
    if user.username:
      USER_MAPPING[user.username.lower().lstrip('@')] = user.id


# === КРИПТОСЕТИ ===
CRYPTO_NETWORKS = {
    "trc20": {
        "name": "USDT (TRC-20)",
        "asset": "USDT",
        "network": "TRC-20",
    },
    "bep20": {
        "name": "USDT (BEP-20)",
        "asset": "USDT",
        "network": "BEP-20",
    },
    "ton": {
        "name": "TON",
        "asset": "TON",
        "network": "TON",
    },
}


def _normalize_network(net_str: str) -> str:
  return re.sub(r"[^a-z0-9]", "", net_str.lower())


def _find_matching_wallets(net_key: str):
  net_info = CRYPTO_NETWORKS.get(net_key)
  if not net_info:
    return []
  target_net = _normalize_network(net_info["network"])
  target_asset = net_info["asset"].upper()
  now = datetime.datetime.now()
  active = []
  for wallet in CRYPTO_WALLETS:
    w_net = _normalize_network(wallet["network"])
    w_asset = wallet["asset"].upper()
    if w_net == target_net and (w_asset == target_asset or net_key == "ton"):
      if wallet["frozen_until"] and wallet["frozen_until"] > now:
        continue
      elif wallet["frozen_until"] and wallet["frozen_until"] <= now:
        wallet["frozen_until"] = None
      active.append(wallet)
  return active


def get_next_crypto_wallet(net_key: str):
  active = _find_matching_wallets(net_key)
  if not active:
    return None
  if net_key not in CRYPTO_POINTERS:
    CRYPTO_POINTERS[net_key] = 0
  index = CRYPTO_POINTERS[net_key] % len(active)
  chosen = active[index]
  CRYPTO_POINTERS[net_key] = (index + 1) % len(active)
  return chosen


# === РЕПУТАЦИЯ ===
def get_reputation_stats():
  current_date = datetime.date.today()
  base_date = datetime.date(2026, 1, 1)
  days_passed = max(0, (current_date - base_date).days)
  completed = 3752 + days_passed * 25
  rng = random.Random(current_date.toordinal())
  in_progress = rng.randint(170, 500)
  new_requests = rng.randint(30, 80)
  return completed, in_progress, new_requests


# === ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ПОИСКА БАННЕРА ===
def get_banner_path(banner_name: str):
  base_name = os.path.splitext(banner_name)[0]
  if os.path.exists("banners"):
    for filename in os.listdir("banners"):
      name_without_ext = os.path.splitext(filename)[0]
      if name_without_ext.lower() == base_name.lower():
        return os.path.join("banners", filename)
  return None


# === 100% ЗАЩИЩЕННАЯ И НАДЕЖНАЯ ФУНКЦИЯ РЕНДЕРИНГА (УСТРАНЕНИЕ ЛЮБЫХ ЗАВИСАНИЙ) ===
async def render_step(event: Message | CallbackQuery, state: FSMContext, banner_name: str, text: str, reply_markup: InlineKeyboardMarkup):
  bot = event.bot
  chat_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
  
  data = await state.get_data()
  bot_msg_id = data.get("bot_msg_id")

  photo_source = None
  banner_path = get_banner_path(banner_name)
  
  if banner_name in BANNER_CACHE:
    photo_source = BANNER_CACHE[banner_name]
  elif banner_path and os.path.exists(banner_path):
    photo_source = FSInputFile(banner_path)

  if isinstance(event, CallbackQuery):
    try:
      await event.answer()
    except Exception:
      pass
    msg = event.message
    try:
      if photo_source:
        try:
          sent_msg = await msg.edit_media(
              media=InputMediaPhoto(media=photo_source, caption=text, parse_mode="HTML"),
              reply_markup=reply_markup
          )
          if banner_name not in BANNER_CACHE and hasattr(sent_msg, "photo") and sent_msg.photo:
            BANNER_CACHE[banner_name] = sent_msg.photo[-1].file_id
          await state.update_data(bot_msg_id=msg.message_id)
          return
        except Exception:
          try:
            await msg.delete()
          except Exception:
            pass
      else:
        try:
          await msg.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
          await state.update_data(bot_msg_id=msg.message_id)
          return
        except Exception:
          try:
            await msg.delete()
          except Exception:
            pass
    except Exception:
      pass

    # Безопасный Fallback для Callback
    try:
      if photo_source:
        new_msg = await bot.send_photo(chat_id=chat_id, photo=photo_source, caption=text, reply_markup=reply_markup, parse_mode="HTML")
        if banner_name not in BANNER_CACHE and new_msg.photo:
          BANNER_CACHE[banner_name] = new_msg.photo[-1].file_id
      else:
        new_msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="HTML")
      await state.update_data(bot_msg_id=new_msg.message_id)
    except Exception as e:
      logger.error(f"Ошибка отправки в render_step (callback fallback): {e}")
  else:
    # Удаляем сообщение пользователя, чтобы чат был чистым
    try:
      await event.delete()
    except Exception:
      pass

    edited_successfully = False
    if bot_msg_id:
      try:
        if photo_source:
          sent_msg = await bot.edit_message_media(
              chat_id=chat_id,
              message_id=bot_msg_id,
              media=InputMediaPhoto(media=photo_source, caption=text, parse_mode="HTML"),
              reply_markup=reply_markup
          )
          if banner_name not in BANNER_CACHE and hasattr(sent_msg, "photo") and sent_msg.photo:
            BANNER_CACHE[banner_name] = sent_msg.photo[-1].file_id
          edited_successfully = True
        else:
          await bot.edit_message_text(
              chat_id=chat_id,
              message_id=bot_msg_id,
              text=text,
              reply_markup=reply_markup,
              parse_mode="HTML"
          )
          edited_successfully = True
      except Exception:
        pass

    # ГАРАНТИРОВАННЫЙ FALLBACK: если редактирование не удалось, всегда отправляем новое сообщение пользователю
    if not edited_successfully:
      try:
        if photo_source:
          new_msg = await bot.send_photo(chat_id=chat_id, photo=photo_source, caption=text, reply_markup=reply_markup, parse_mode="HTML")
          if banner_name not in BANNER_CACHE and new_msg.photo:
            BANNER_CACHE[banner_name] = new_msg.photo[-1].file_id
        else:
          new_msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="HTML")
        await state.update_data(bot_msg_id=new_msg.message_id)
      except Exception as e:
        logger.error(f"Ошибка отправки нового сообщения в render_step (message fallback): {e}")


# === ГЛАВНОЕ МЕНЮ (КИБЕР) С ИНВЕРТИРОВАННЫМИ ЦВЕТАМИ КНОПОК ===
async def show_main_menu(event: Message | CallbackQuery, state: FSMContext):
  await state.clear()
  user_id = event.from_user.id
  if user_id not in KNOWN_USERS:
    KNOWN_USERS[user_id] = datetime.datetime.now()

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🛒 Заказать", callback_data="catalog_services", style="primary")],
          [
              InlineKeyboardButton(text="📦 В работе", callback_data="my_orders", style="danger"),
              InlineKeyboardButton(text="📊 Репутация", callback_data="about_service", style="danger"),
          ],
          [InlineKeyboardButton(text="💬 Поддержка", callback_data="contact_admin", style="primary")],
      ]
  )
  text = (
      "💻 <b>ARENA SHOP — КИБЕР</b>\n\n"
      "<i>Добро пожаловать в сервис. Все конфиденциально, быстро и безопасно. Работаем 24/7.</i>\n\n"
      "<b>👇 Выберите пункт меню ниже:</b>"
  )
  await render_step(event, state, "main.jpg", text, keyboard)


# === АДМИНСКИЕ И МЕНЕДЖЕРСКИЕ КОМАНДЫ ===
@router.message(Command("bill"))
async def cmd_manager_bill(message: Message):
  if not is_manager_authorized(message.from_user.id):
    return
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply("❌ Использование: `/bill <user_id> <услуга> <сумма$>`\nПример: `/bill 282810030 особый заказ 220$`", parse_mode="Markdown")
    return

  parts = args[1].strip().split()
  if len(parts) < 2 or not parts[0].isdigit():
    await message.reply("❌ Неверный формат. Пример: `/bill 282810030 особый заказ 220$`", parse_mode="Markdown")
    return

  target_user_id = int(parts[0])
  raw_amount = parts[-1].rstrip("$").strip()
  try:
    amount = float(raw_amount)
  except ValueError:
    await message.reply("❌ Неверная сумма. Пример: `/bill 282810030 особый заказ 220$`", parse_mode="Markdown")
    return

  service_name = " ".join(parts[1:-1]).strip() if len(parts) > 2 else "Особый заказ"
  if not service_name:
    service_name = "Особый заказ"

  PENDING_BILLS[target_user_id] = {
      "service_name": service_name,
      "total_price": amount,
      "target_username": "По счету менеджера",
  }

  price_str = _format_price(amount)
  bill_text = (
      f"🧾 <b>Вам выставлен счет на {service_name}</b>\n\n"
      f"• Услуга: <b>{service_name}</b>\n"
      f"• К оплате: <b>{price_str} USDT</b>\n\n"
      f"Выберите способ оплаты ниже, чтобы получить реквизиты:"
  )
  try:
    await message.bot.send_message(
        chat_id=target_user_id,
        text=bill_text,
        reply_markup=_build_payment_keyboard(),
        parse_mode="HTML",
    )
    await message.reply(
        f"✅ Счет на <b>{service_name}</b> (<b>{price_str} USDT</b>) отправлен пользователю <code>{target_user_id}</code>.",
        parse_mode="HTML",
    )
  except Exception as e:
    await message.reply(f"❌ Не удалось отправить счет пользователю <code>{target_user_id}</code>: {e}", parse_mode="HTML")


@router.message(Command("send"))
async def cmd_manager_send(message: Message):
  if not is_manager_authorized(message.from_user.id):
    return
  args = message.text.split(maxsplit=2)
  if len(args) < 3 or not args[1].strip().isdigit():
    await message.reply("❌ Использование: `/send <user_id> <сообщение>`\nПример: `/send 282810030 Привет! Счет готов.`", parse_mode="Markdown")
    return

  target_user_id = int(args[1].strip())
  out_text = args[2].strip()
  if not out_text:
    await message.reply("❌ Сообщение не может быть пустым.")
    return

  try:
    await message.bot.send_message(
        chat_id=target_user_id,
        text=f"💬 <b>Сообщение от менеджера:</b>\n\n{out_text}",
        parse_mode="HTML",
    )
    await message.reply(f"✅ Сообщение отправлено пользователю <code>{target_user_id}</code>.", parse_mode="HTML")
  except Exception as e:
    await message.reply(f"❌ Ошибка отправки пользователю <code>{target_user_id}</code>: {e}", parse_mode="HTML")


@router.message(Command("ban"))
async def cmd_ban_user(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply("❌ Использование: `/ban <user_id или @username>`", parse_mode="Markdown")
    return
  target = args[1].strip()
  target_user_id = int(target) if target.isdigit() else USER_MAPPING.get(target.lstrip("@").lower())
  if target_user_id:
    ban_user_in_db(target_user_id)
    await message.reply(f"✅ Пользователь с ID <code>{target_user_id}</code> заблокирован.", parse_mode="HTML")
  else:
    await message.reply("❌ Пользователь не найден.")


@router.message(Command("ans"))
async def cmd_admin_answer(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  args = message.text.split(maxsplit=2)
  if len(args) < 3:
    await message.reply("❌ Использование: `/ans <user_id> <текст ответа>`", parse_mode="Markdown")
    return
  target_user_id_str = args[1].strip()
  reply_text = args[2].strip()
  if not target_user_id_str.isdigit():
    await message.reply("❌ Неверный ID пользователя.")
    return
  target_user_id = int(target_user_id_str)
  try:
    await message.bot.send_message(
        chat_id=target_user_id,
        text=f"💬 <b>Сообщение от службы поддержки:</b>\n\n{reply_text}",
        parse_mode="HTML"
    )
    await message.reply(f"✅ Ответ успешно отправлен пользователю <code>{target_user_id}</code>.", parse_mode="HTML")
  except Exception as e:
    await message.reply(f"❌ Ошибка отправки: {e}")


@router.message(F.from_user.id == ADMIN_ID, F.reply_to_message)
async def admin_reply_handler(message: Message):
  reply_msg = message.reply_to_message
  text_to_parse = (reply_msg.text or "") + (reply_msg.caption or "")
  
  match = re.search(r"\(?<code>(\d+)<\/code>\)?", text_to_parse)
  if not match:
    match = re.search(r"ID:\s*(\d+)", text_to_parse)
  if not match:
    match = re.search(r"\b(\d{6,12})\b", text_to_parse)
    
  if match:
    target_user_id = int(match.group(1))
    try:
      await message.bot.send_message(
          chat_id=target_user_id,
          text=f"💬 <b>Сообщение от службы поддержки:</b>\n\n{message.text}",
          parse_mode="HTML"
      )
      await message.reply(f"✅ Ответ отправлен пользователю <code>{target_user_id}</code>.", parse_mode="HTML")
    except Exception as e:
      await message.reply(f"❌ Не удалось отправить сообщение пользователю: {e}")
  else:
    await message.reply("❌ Не удалось определить ID пользователя из сообщения, на которое вы ответили.")


@router.message(Command("clear_cache"))
async def cmd_clear_cache(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  BANNER_CACHE.clear()
  await message.reply("✅ Кеш баннеров успешно очищен.")


def _parse_wallet_args(raw: str):
  parts = raw.strip().split()
  if len(parts) < 3:
    return None
  address = parts[0]
  asset = parts[1].upper()
  network = " ".join(parts[2:])
  return address, asset, network


@router.message(Command("add"))
async def cmd_add_wallet(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply("❌ Использование: `/add <номеркриптокошелька> <монета> <сеть>`\nПример: `/add TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe USDT TRC-20`", parse_mode="Markdown")
    return
  parsed = _parse_wallet_args(args[1])
  if not parsed:
    await message.reply("❌ Неверный формат. Пример: `/add TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe USDT TRC-20`", parse_mode="Markdown")
    return
  address, asset, network = parsed
  CRYPTO_WALLETS.append({
      "address": address,
      "asset": asset,
      "network": network,
      "frozen_until": None,
  })
  await message.reply(
      f"✅ Реквизиты добавлены!\n🪙 {asset} | 🌐 {network}\n<code>{address}</code>",
      parse_mode="HTML",
  )


@router.message(Command("delete"))
async def cmd_delete_wallet(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  global CRYPTO_WALLETS
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply("❌ Использование: `/delete <номеркриптокошелька> <монета> <сеть>`", parse_mode="Markdown")
    return
  parsed = _parse_wallet_args(args[1])
  if not parsed:
    await message.reply("❌ Неверный формат.")
    return
  address, asset, network = parsed
  target_net = _normalize_network(network)
  initial_len = len(CRYPTO_WALLETS)
  CRYPTO_WALLETS = [
      w for w in CRYPTO_WALLETS
      if not (
          w["address"] == address
          and w["asset"].upper() == asset
          and _normalize_network(w["network"]) == target_net
      )
  ]
  if len(CRYPTO_WALLETS) < initial_len:
    await message.reply("✅ Удалено.")
  else:
    await message.reply("❌ Не найдено.")


@router.message(Command("freeze"))
async def cmd_freeze_wallet(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply("❌ Использование: `/freeze <номеркриптокошелька> <монета> <сеть>`", parse_mode="Markdown")
    return
  parsed = _parse_wallet_args(args[1])
  if not parsed:
    await message.reply("❌ Неверный формат.")
    return
  address, asset, network = parsed
  target_net = _normalize_network(network)
  found = False
  now = datetime.datetime.now()
  for w in CRYPTO_WALLETS:
    if (
        w["address"] == address
        and w["asset"].upper() == asset
        and _normalize_network(w["network"]) == target_net
    ):
      w["frozen_until"] = now + datetime.timedelta(hours=24)
      found = True
      break
  if found:
    await message.reply("✅ Зафрижено на 24 часа.")
  else:
    await message.reply("❌ Не найдено.")


# === CRYPTOBOT ИНТЕГРАЦИЯ ===
async def create_cryptobot_invoice(amount_usd: float, description: str):
  if not CRYPTO_BOT_TOKEN:
    return "", None
  url = "https://pay.crypt.bot/api/createInvoice"
  headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
  payload = {"asset": "USDT", "amount": str(amount_usd), "description": description, "payload": "order_payment"}
  try:
    async with ClientSession() as session:
      async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
        if resp.status == 200:
          data = await resp.json()
          if data.get("ok"):
            res = data["result"]
            return res["pay_url"], res["invoice_id"]
  except Exception as e:
    logger.error(f"Ошибка CryptoBot: {e}")
  return "", None


async def check_cryptobot_invoice_status(invoice_id: int) -> bool:
  if not CRYPTO_BOT_TOKEN or not invoice_id:
    return True
  url = "https://pay.crypt.bot/api/getInvoices"
  headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
  params = {"invoice_ids": str(invoice_id)}
  try:
    async with ClientSession() as session:
      async with session.get(url, params=params, headers=headers, timeout=10) as resp:
        if resp.status == 200:
          data = await resp.json()
          if data.get("ok"):
            items = data.get("result", {}).get("items", [])
            if items:
              return items[0].get("status") == "paid"
  except Exception as e:
    logger.error(f"Ошибка проверки инвойса: {e}")
  return False


# === ФОНОВЫЕ ЗАДАЧИ ===
async def scheduled_broadcast(bot: Bot):
  while True:
    await asyncio.sleep(3600)
    now = datetime.datetime.now()
    banner_path = get_banner_path("special_offer.jpg")
    for user_id, last_time in list(KNOWN_USERS.items()):
      if is_user_banned(user_id):
        continue
      if last_time is None or (now - last_time).total_seconds() >= 24 * 3600:
        try:
          today = datetime.date.today()
          target_date = today + datetime.timedelta(days=3)
          months_ru = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
          text = (
              "✨ <b>Специальное предложение</b>\n\n"
              f"Действует персональная скидка <b>20%</b> до <b>{target_date.day} {months_ru[target_date.month]}</b>.\n\n"
              "Ждем вас в боте 👇"
          )
          keyboard = InlineKeyboardMarkup(inline_keyboard=[
              [InlineKeyboardButton(text="✨ Оформить со скидкой", callback_data="catalog_services", style="primary")],
              [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home", style="danger")]
          ])
          
          if "special_offer.jpg" in BANNER_CACHE:
            photo = BANNER_CACHE["special_offer.jpg"]
            await bot.send_photo(chat_id=user_id, photo=photo, caption=text, reply_markup=keyboard, parse_mode="HTML")
          elif banner_path and os.path.exists(banner_path):
            photo = FSInputFile(banner_path)
            sent = await bot.send_photo(chat_id=user_id, photo=photo, caption=text, reply_markup=keyboard, parse_mode="HTML")
            if sent.photo:
              BANNER_CACHE["special_offer.jpg"] = sent.photo[-1].file_id
          else:
            await bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard, parse_mode="HTML")
            
          KNOWN_USERS[user_id] = now
        except Exception as e:
          logger.error(f"Ошибка рассылки: {e}")


async def delayed_abandoned_cart_reminder(bot: Bot, user_id: int):
  try:
    await asyncio.sleep(1800)
    if is_user_banned(user_id):
      return
    if user_id in ABANDONED_CHECKOUTS:
      reminder_text = "👋 <b>Завершить заказ со скидкой -20%?</b>\n\nВы остановились на этапе оплаты. Возвращайтесь к оформлению 👇"
      keyboard = InlineKeyboardMarkup(inline_keyboard=[
          [InlineKeyboardButton(text="🛒 Вернуться к заказу", callback_data="catalog_services", style="primary")],
          [InlineKeyboardButton(text="💬 Поддержка", callback_data="contact_admin", style="primary")]
      ])
      
      banner_path = get_banner_path("abandoned_cart.jpg")
      if "abandoned_cart.jpg" in BANNER_CACHE:
        photo = BANNER_CACHE["abandoned_cart.jpg"]
        await bot.send_photo(chat_id=user_id, photo=photo, caption=reminder_text, reply_markup=keyboard, parse_mode="HTML")
      elif banner_path and os.path.exists(banner_path):
        photo = FSInputFile(banner_path)
        sent = await bot.send_photo(chat_id=user_id, photo=photo, caption=reminder_text, reply_markup=keyboard, parse_mode="HTML")
        if sent.photo:
          BANNER_CACHE["abandoned_cart.jpg"] = sent.photo[-1].file_id
      else:
        await bot.send_message(chat_id=user_id, text=reminder_text, reply_markup=keyboard, parse_mode="HTML")
        
      ABANDONED_CHECKOUTS.pop(user_id, None)
  except asyncio.CancelledError:
    pass


def get_order_details_text(data):
  return f"• Цель: <b>{data.get('target_username')}</b>\n"


# === ОБРАБОТЧИКИ КОМАНД ===
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
  track_user(message.from_user)
  await show_main_menu(message, state)


@router.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext):
  await message.reply("🔄 Бот перезапущен, состояние очищено.")
  await show_main_menu(message, state)


@router.message(Command("order"))
async def cmd_order(message: Message, state: FSMContext):
  track_user(message.from_user)
  user_id = message.from_user.id
  if user_id not in KNOWN_USERS:
    KNOWN_USERS[user_id] = datetime.datetime.now()
  await state.set_state(OrderForm.service)
  text = "🛒 <b>Каталог услуг</b>\n\nВыберите подходящий вариант из списка ниже:"
  await render_step(message, state, "catalog.jpg", text, _build_catalog_keyboard())


@router.message(Command("my_orders"))
async def cmd_my_orders_msg(message: Message, state: FSMContext):
  track_user(message.from_user)
  user_id = message.from_user.id
  orders = get_user_orders_from_db(user_id)
  if not orders:
    text = "📦 <b>Мои заказы</b>\n\nУ вас пока нет активных или завершенных заказов."
  else:
    text = "📦 <b>История ваших заказов:</b>\n\n"
    for ord_data in orders:
      text += f"<b>Заказ #{ord_data['id']}</b>\n• Услуга: {ord_data['service_name']}\n• Сумма: {ord_data['total_price']}$\n• Статус: {ord_data['status']}\n\n"
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_home", style="danger")]])
  await render_step(message, state, "my_orders.jpg", text, keyboard)


@router.message(Command("reputation"))
async def cmd_reputation_msg(message: Message, state: FSMContext):
  track_user(message.from_user)
  completed, in_progress, new_requests = get_reputation_stats()
  text = (
      "📊 <b>Репутация сервиса</b>\n\n"
      f"• Успешно выполнено задач: <b>{completed:,}</b>\n"
      f"• В активной работе: <b>{in_progress}</b>\n"
      f"• Новых заявок сегодня: <b>{new_requests}</b>\n\n"
      "<i>Конфиденциальность и безопасность гарантированы.</i>"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_home", style="danger")]])
  await render_step(message, state, "reputation.jpg", text, keyboard)


@router.callback_query(F.data == "back_home")
async def back_home(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  task = ABANDONED_CHECKOUTS.pop(user_id, None)
  if task:
    task.cancel()
  await show_main_menu(callback, state)


@router.callback_query(F.data == "about_service")
async def show_reputation(callback: CallbackQuery, state: FSMContext):
  completed, in_progress, new_requests = get_reputation_stats()
  text = (
      "📊 <b>Репутация сервиса</b>\n\n"
      f"• Успешно выполнено задач: <b>{completed:,}</b>\n"
      f"• В активной работе: <b>{in_progress}</b>\n"
      f"• Новых заявок сегодня: <b>{new_requests}</b>\n\n"
      "<i>Конфиденциальность и безопасность гарантированы.</i>"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_home", style="danger")]])
  await render_step(callback, state, "reputation.jpg", text, keyboard)


@router.callback_query(F.data == "my_orders")
async def show_my_orders(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  orders = get_user_orders_from_db(user_id)
  if not orders:
    text = "📦 <b>Мои заказы</b>\n\nУ вас пока нет активных или завершенных заказов."
  else:
    text = "📦 <b>История ваших заказов:</b>\n\n"
    for ord_data in orders:
      text += f"<b>Заказ #{ord_data['id']}</b>\n• Услуга: {ord_data['service_name']}\n• Сумма: {ord_data['total_price']}$\n• Статус: {ord_data['status']}\n\n"
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_home", style="danger")]])
  await render_step(callback, state, "my_orders.jpg", text, keyboard)


@router.callback_query(F.data == "contact_admin")
async def contact_admin_info(callback: CallbackQuery, state: FSMContext):
  await state.set_state(SupportForm.reason)
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home", style="primary")]])
  text = (
      "💬 <b>Служба поддержки</b>\n\n"
      "Опишите вашу ситуацию или задайте вопрос (оплата, статус заказа и т.д.):"
  )
  await render_step(callback, state, "support.jpg", text, keyboard)


@router.message(SupportForm.reason, F.text)
async def process_support_reason(message: Message, state: FSMContext):
  track_user(message.from_user)
  await state.update_data(reason=message.text.strip())
  await state.set_state(SupportForm.photo)
  keyboard = InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text="⏩ Пропустить", callback_data="skip_support_photo", style="danger")],
      [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home", style="primary")]
  ])
  text = "📸 Отправьте фото при необходимости или нажмите пропустить:"
  await render_step(message, state, "support.jpg", text, keyboard)


@router.message(SupportForm.photo, F.photo)
async def process_support_photo(message: Message, state: FSMContext):
  track_user(message.from_user)
  photo_id = message.photo[-1].file_id
  data = await state.get_data()
  user = message.from_user
  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  reason_text = data.get("reason") or "Без описания"
  admin_text = f"📩 <b>Новое обращение в поддержку</b>\n\n• От кого: {user_info} (<code>{user.id}</code>)\n• Вопрос: {reason_text}"
  notify_ids = {uid for uid in (ADMIN_ID, MANAGER_ID) if uid}
  for uid in notify_ids:
    try:
      await message.bot.send_photo(chat_id=uid, photo=photo_id, caption=admin_text, parse_mode="HTML")
    except Exception:
      pass
  text = "✅ Обращение отправлено! Специалист скоро свяжется с вами."
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home", style="primary")]])
  await render_step(message, state, "main.jpg", text, keyboard)
  await state.clear()


@router.callback_query(SupportForm.photo, F.data == "skip_support_photo")
async def skip_support_photo(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  data = await state.get_data()
  user = callback.from_user
  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  reason_text = data.get("reason") or (callback.message.caption if callback.message and callback.message.caption else "Обращение без текста")
  admin_text = f"📩 <b>Новое обращение в поддержку</b>\n\n• От кого: {user_info} (<code>{user.id}</code>)\n• Вопрос: {reason_text}"
  notify_ids = {uid for uid in (ADMIN_ID, MANAGER_ID) if uid}
  for uid in notify_ids:
    try:
      await callback.bot.send_message(chat_id=uid, text=admin_text, parse_mode="HTML")
    except Exception as e:
      logger.error(f"Ошибка отправки обращения в поддержку ({uid}): {e}")

  text = "✅ Обращение отправлено! Специалист скоро свяжется с вами."
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home", style="primary")]])
  await state.clear()
  await render_step(callback, state, "main.jpg", text, keyboard)


# === КАТАЛОГ И ОФОРМЛЕНИЕ ЗАКАЗА ===
@router.callback_query(F.data.in_(["catalog_services", "start_order"]))
async def process_start_order(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  task = ABANDONED_CHECKOUTS.pop(user_id, None)
  if task:
    task.cancel()

  await state.set_state(OrderForm.service)
  text = "🛒 <b>Каталог услуг</b>\n\nВыберите подходящий вариант из списка ниже:"
  await render_step(callback, state, "catalog.jpg", text, _build_catalog_keyboard())


@router.callback_query(OrderForm.service, F.data == "srv_special_order")
async def process_special_order_service(callback: CallbackQuery, state: FSMContext):
  service_name = "Особый заказ — Рассчитать стоимость"
  await state.update_data(service_name=service_name, total_price=0)
  await state.set_state(OrderForm.target_username)

  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog_services", style="danger")]])
  text = (
      f"🛠 Услуга: <b>{service_name}</b>\n\n"
      f"📝 <b>Описание задачи</b>\n\n"
      f"Опишите задачу следующим сообщением:"
  )
  await render_step(callback, state, "target_data.jpg", text, keyboard)


@router.callback_query(OrderForm.service, F.data.startswith("srv_"))
async def process_service(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  price = int(parts[-1])
  service_name = " ".join(parts[1:-1]).replace("_", " ")

  await state.update_data(service_name=service_name, total_price=price)
  await state.set_state(OrderForm.target_username)

  link_services = ["снос сайта", "снос tg канала или бота", "ddos-атака сайта"]
  if service_name.lower() in link_services:
    target_prompt = "Введите ссылку на ресурс цели следующим сообщением:"
  else:
    target_prompt = "Введите юзернейм или ссылку на профиль цели следующим сообщением:"

  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog_services", style="danger")]])
  text = (
      f"⚡️ Услуга: <b>{service_name} ({price}$)</b>\n\n"
      f"🎯 <b>Данные цели</b>\n\n"
      f"{target_prompt}"
  )
  await render_step(callback, state, "target_data.jpg", text, keyboard)


@router.message(OrderForm.target_username, F.text)
async def process_target_username(message: Message, state: FSMContext):
  track_user(message.from_user)
  data = await state.get_data()
  price = data.get("total_price", 0)

  if not price:
    await state.update_data(special_task=message.text.strip())
    await state.set_state(OrderForm.special_link)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog_services", style="danger")]])
    text = (
        f"🛠 Услуга: <b>{data.get('service_name')}</b>\n\n"
        f"🔗 <b>Ссылка на цель</b>\n\n"
        f"Отправьте ссылку на цель следующим сообщением:"
    )
    await render_step(message, state, "target_data.jpg", text, keyboard)
    return

  await state.update_data(target_username=message.text.strip())
  updated_data = await state.get_data()
  summary_text = (
      "✨ <b>Сводка заказа:</b>\n\n"
      f"• Услуга: <b>{updated_data.get('service_name')}</b>\n"
      f"{get_order_details_text(updated_data)}"
      f"💵 <b>К оплате: {_format_price(price)} USDT</b>"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text="💳 Перейти к оплате", callback_data="go_to_payment", style="primary")],
      [InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog_services", style="danger")]
  ])
  await render_step(message, state, "summary.jpg", summary_text, keyboard)


@router.message(OrderForm.special_link, F.text)
async def process_special_order_link(message: Message, state: FSMContext):
  track_user(message.from_user)
  link = message.text.strip()
  data = await state.get_data()
  task_desc = data.get("special_task", "")
  user = message.from_user
  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"

  notify_ids = {uid for uid in (ADMIN_ID, MANAGER_ID) if uid}
  for uid in notify_ids:
    try:
      await message.bot.send_message(
          chat_id=uid,
          text=(
              f"🛠 <b>Заявка на особый заказ (Рассчитать стоимость)</b>\n\n"
              f"• Клиент: {user_info} (<code>{user.id}</code>)\n"
              f"• Задача: {task_desc}\n"
              f"• Ссылка: {link}\n\n"
              f"Выставить счёт: <code>/bill {user.id} особый заказ 220$</code>"
          ),
          parse_mode="HTML",
      )
    except Exception:
      pass

  text = "✅ Заявка отправлена менеджеру! В течение 25 минут с вами свяжется специалист для расчёта стоимости."
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home", style="primary")]])
  await state.clear()
  await render_step(message, state, "main.jpg", text, keyboard)


@router.callback_query(F.data == "go_to_payment")
async def process_go_to_payment(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  ctx = await _get_payment_context(user_id, state)
  total_price = ctx["total_price"]

  if user_id in ABANDONED_CHECKOUTS:
    task = ABANDONED_CHECKOUTS.pop(user_id, None)
    if task:
      task.cancel()
  ABANDONED_CHECKOUTS[user_id] = asyncio.create_task(delayed_abandoned_cart_reminder(callback.bot, user_id))

  payment_text = (
      "⏳ <b>Оплата заказа</b>\n"
      "Время на завершение: <b><u>20:00</u></b>\n\n"
      f"💳 <b>Выберите способ оплаты</b>\n"
      f"К оплате: <b>{_format_price(total_price)} USDT</b>\n\n"
      "📖 <b>Мини-гайд: как быстро и безопасно оплатить через CryptoBot</b>\n"
      "1. Зайдите в @CryptoBot и откройте <b>P2P Маркет</b>\n"
      "2. Пополните баланс <b>USDT</b> в любой удобной валюте вашей страны\n"
      "3. Вернитесь в нашего бота и нажмите <b>«Оплата через CryptoBot»</b>\n"
      "4. Нажмите <b>«Оплатить счет»</b> и подтвердите платеж — готово ✅"
  )
  await render_step(callback, state, "payment.jpg", payment_text, _build_payment_keyboard())


@router.callback_query(F.data == "payment_help_manager")
async def process_payment_help_manager(callback: CallbackQuery):
  notify_ids = {uid for uid in (ADMIN_ID, MANAGER_ID) if uid}
  for uid in notify_ids:
    try:
      user = callback.from_user
      await callback.bot.send_message(chat_id=uid, text=f"🚨 Запрос помощи с оплатой от @{user.username} (<code>{user.id}</code>)", parse_mode="HTML")
    except Exception:
      pass
  await callback.answer("✅ Менеджер уведомлен, скоро свяжется с вами.", show_alert=True)


@router.callback_query(F.data == "pay_cryptobot")
async def process_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
  ctx = await _get_payment_context(callback.from_user.id, state)
  total_price = ctx["total_price"]

  pay_url, inv_id = await create_cryptobot_invoice(total_price, "Оплата услуг")
  if not pay_url:
    await callback.answer("❌ Сервис недоступен.", show_alert=True)
    return

  await state.update_data(invoice_id=inv_id)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔗 Оплатить счет", url=pay_url, style="primary")],
          [InlineKeyboardButton(text="✅ Проверить оплату", callback_data="cryptobot_check_success", style="danger")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment", style="danger")],
      ]
  )
  text = f"🤖 <b>Оплата через CryptoBot</b>\n\nК оплате: <b>{_format_price(total_price)} USDT</b>\n\nПосле оплаты нажмите кнопку проверки:"
  await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data == "cryptobot_check_success")
async def process_cryptobot_check(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  inv_id = data.get("invoice_id")
  
  is_paid = await check_cryptobot_invoice_status(inv_id)
  if not is_paid:
    await callback.answer("⏳ Платеж еще не поступил. Попробуйте через минуту.", show_alert=True)
    return

  user = callback.from_user
  task = ABANDONED_CHECKOUTS.pop(user.id, None)
  if task:
    task.cancel()

  ctx = await _get_payment_context(user.id, state)
  service_name = ctx["service_name"]
  total_price = ctx["total_price"]
  target_username = ctx["target_username"]
  target_info = f"Цель: {target_username}"

  add_order_to_db(user.id, service_name, total_price, "✅ Оплачено через CryptoBot", target_info)
  PENDING_BILLS.pop(user.id, None)

  notify_ids = {uid for uid in (ADMIN_ID, MANAGER_ID) if uid}
  for uid in notify_ids:
    try:
      await callback.bot.send_message(
          chat_id=uid,
          text=f"🤖 Успешная оплата CryptoBot от @{user.username} (<code>{user.id}</code>)\n• Услуга: {service_name}\n• Цель: {target_username}",
          parse_mode="HTML"
      )
    except Exception:
      pass

  text = "✅ <b>Оплата подтверждена!</b>\n\nВаша заявка принята в работу."
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home", style="primary")]])
  await render_step(callback, state, "main.jpg", text, keyboard)
  await state.clear()


@router.callback_query(F.data == "crypto_expert")
async def process_crypto_expert(callback: CallbackQuery, state: FSMContext):
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="💎 USDT (TRC-20)", callback_data="pay_crypto_trc20", style="primary")],
          [InlineKeyboardButton(text="💵 USDT (BEP-20)", callback_data="pay_crypto_bep20", style="danger")],
          [InlineKeyboardButton(text="🪙 TON", callback_data="pay_crypto_ton", style="primary")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment", style="danger")],
      ]
  )
  text = "🪙 <b>Реквизиты (Крипта)</b>\n\nВыберите криптовалютную сеть для перевода:"
  await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data.startswith("pay_crypto_"))
async def process_pay_crypto(callback: CallbackQuery, state: FSMContext):
  net_key = callback.data.split("_")[2]
  net_info = CRYPTO_NETWORKS.get(net_key)
  if not net_info:
    await callback.answer("Сеть не найдена", show_alert=True)
    return

  ctx = await _get_payment_context(callback.from_user.id, state)
  total_price = ctx["total_price"]
  wallet = get_next_crypto_wallet(net_key)

  if not wallet:
    text = (
        "❌ <b>Реквизиты временно недоступны</b>\n\n"
        "Сейчас реквизиты временно недоступны, вернитесь к оплате через 5 минут — мы эту проблему решаем."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Оплата через CryptoBot", callback_data="pay_cryptobot", style="primary")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="crypto_expert", style="danger")],
        ]
    )
    await render_step(callback, state, "payment.jpg", text, keyboard)
    return

  text = (
      f"💳 <b>Оплата {net_info['name']}</b>\n\n"
      f"• К оплате: <b>{_format_price(total_price)} USDT</b>\n"
      f"• Монета: <b>{wallet['asset']}</b>\n"
      f"• Сеть: <b>{wallet['network']}</b>\n"
      f"• Реквизиты:\n<code>{wallet['address']}</code>\n\n"
      "💡 <i>После перевода прикрепите чек.</i>"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text="✅ Прикрепить чек", callback_data="ask_for_receipt", style="primary")],
      [InlineKeyboardButton(text="⬅️ Назад", callback_data="crypto_expert", style="danger")]
  ])
  await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data == "ask_for_receipt")
async def ask_for_receipt(callback: CallbackQuery, state: FSMContext):
  await state.set_state(OrderForm.receipt)
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment", style="danger")]])
  text = "🧾 Отправьте скриншот или фото чека следующим сообщением:"
  await render_step(callback, state, "payment.jpg", text, keyboard)


@router.message(OrderForm.receipt, F.photo)
async def process_receipt_photo(message: Message, state: FSMContext):
  user = message.from_user
  task = ABANDONED_CHECKOUTS.pop(user.id, None)
  if task:
    task.cancel()

  ctx = await _get_payment_context(user.id, state)
  service_name = ctx["service_name"]
  total_price = ctx["total_price"]
  target_username = ctx["target_username"]
  target_info = f"Цель: {target_username}"

  add_order_to_db(user.id, service_name, total_price, "⏳ Ожидает проверки чека", target_info)
  PENDING_BILLS.pop(user.id, None)

  notify_ids = {uid for uid in (ADMIN_ID, MANAGER_ID) if uid}
  for uid in notify_ids:
    try:
      await message.bot.send_photo(
          chat_id=uid,
          photo=message.photo[-1].file_id,
          caption=f"🛒 Новый чек от @{user.username} (<code>{user.id}</code>)\n• Услуга: {service_name}\n• Цель: {target_username}\n• К оплате: {_format_price(total_price)} USDT",
          parse_mode="HTML"
      )
    except Exception:
      pass

  text = "✅ Чек получен! Менеджер проверит платеж в течение 25 минут."
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home", style="primary")]])
  await render_step(callback, state, "main.jpg", text, keyboard)
  await state.clear()


# === ЗАЩИТНЫЕ (FALLBACK) ХЕНДЛЕРЫ ===
@router.message(OrderForm.target_username)
async def fallback_target_username(message: Message):
  await message.reply("⚠️ Пожалуйста, отправьте текстовое сообщение с описанием задачи.")

@router.message(OrderForm.special_link)
async def fallback_special_link(message: Message):
  await message.reply("⚠️ Пожалуйста, отправьте текстовое сообщение со ссылкой на цель.")

@router.message(OrderForm.receipt)
async def fallback_order_receipt(message: Message):
  await message.reply("⚠️ Пожалуйста, отправьте скриншот или фото чека об оплате.")

@router.message(SupportForm.reason)
async def fallback_support_message(message: Message):
  await message.reply("⚠️ Пожалуйста, отправьте ваше сообщение текстовым форматом.")


# === WEB SERVER FOR RENDER / RAILWAY ===
async def handle_ping(request):
  return web.Response(text="Bot is running!")

async def start_web_server():
  app = web.Application()
  app.router.add_get("/", handle_ping)
  runner = web.AppRunner(app)
  await runner.setup()
  port = int(os.getenv("PORT", 10000))
  site = web.TCPSite(runner, "0.0.0.0", port)
  await site.start()


async def main():
  init_db()
  bot = Bot(token=BOT_TOKEN)
  dp = Dispatcher()
  dp.include_router(router)
  
  try:
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="restart", description="🔄 Перезапуск / Сброс бота"),
        BotCommand(command="order", description="Заказать услугу"),
        BotCommand(command="my_orders", description="Мои заказы"),
        BotCommand(command="reputation", description="Репутация"),
    ])
  except Exception as e:
    logger.warning(f"Не удалось установить команды: {e}")
  
  asyncio.create_task(start_web_server())
  asyncio.create_task(scheduled_broadcast(bot))
  await bot.delete_webhook(drop_pending_updates=True)
  
  while True:
    try:
      logger.info("Запуск поллинга кибер-бота...")
      await dp.start_polling(bot)
    except Exception as e:
      logger.error(f"Ошибка поллинга: {e}. Переподключение через 5 секунд...")
      await asyncio.sleep(5)


if __name__ == "__main__":
  asyncio.run(main())
