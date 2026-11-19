import asyncio
import datetime
import logging
import os
import random
import re
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiohttp import ClientSession, web
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
raw_xrocket = os.getenv("XROCKET_TOKEN") or os.getenv("ROCKET_PAY_KEY") or ""
XROCKET_TOKEN = re.sub(r"\s+", "", raw_xrocket.strip().strip('"').strip("'"))
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")

if not BOT_TOKEN:
  raise ValueError("❌ BOT_TOKEN не найден!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

admin_env = os.getenv("ADMIN_ID")
ADMIN_ID = int(admin_env) if admin_env and admin_env.isdigit() else None

manager_env = os.getenv("MANAGER_ID")
MANAGER_ID = int(manager_env) if manager_env and manager_env.isdigit() else None


class OrderForm(StatesGroup):
  service = State()
  debt_info = State()
  debt_wallet = State()
  special_task = State()
  country = State()
  target_location = State()
  target_data = State()
  photos = State()
  urgency = State()
  receipt = State()


class SupportForm(StatesGroup):
  reason = State()
  photo = State()


# Базы данных в памяти
USER_ORDERS = {}
USER_LAST_COUNTRY = {}
USER_BILLS = {}
KNOWN_USERS = {}
ABANDONED_CHECKOUTS = {}
CRYPTO_WALLETS = []
WALLET_POINTERS = {}
BANNED_USERS = set()
BANNER_CACHE = {}
PAID_INVOICES = set()
INVOICE_TO_USER = {}

BOT_INSTANCE: Bot | None = None
router = Router()


# === MIDDLEWARE ДЛЯ ПРОВЕРКИ БАНА ===
@router.message.middleware()
async def ban_middleware(handler, event, data):
  if event.from_user and event.from_user.id in BANNED_USERS:
    return
  return await handler(event, data)


@router.callback_query.middleware()
async def ban_callback_middleware(handler, event, data):
  if event.from_user and event.from_user.id in BANNED_USERS:
    await event.answer("Вы заблокированы в этом боте.", show_alert=True)
    return
  return await handler(event, data)


# === СЛОВАРЬ ВАЛЮТ ДЛЯ СТРАН ===
COUNTRY_CURRENCIES = {
    "Россия": {"code": "RUB", "symbol": "рублей"},
    "Беларусь": {"code": "BYN", "symbol": "BYN"},
    "Украина": {"code": "UAH", "symbol": "грн."},
    "Казахстан": {"code": "KZT", "symbol": "тенге"},
    "Узбекистан": {"code": "UZS", "symbol": "сум"},
    "Молдова": {"code": "MDL", "symbol": "лей"},
    "Кыргызстан": {"code": "KGS", "symbol": "сом"},
    "Азербайджан": {"code": "AZN", "symbol": "AZN"},
    "Армения": {"code": "AMD", "symbol": "драм"},
    "Грузия": {"code": "GEL", "symbol": "лари"},
}

# === КРИПТОСЕТИ ===
CRYPTO_NETWORKS = {
    "trc20": {"coin": "USDT", "name": "USDT (TRC-20)", "network": "TRC-20"},
    "bep20": {"coin": "USDT", "name": "USDT (BEP-20)", "network": "BEP-20"},
    "ton": {"coin": "TON", "name": "TON", "network": "TON"},
}


def normalize_network(net: str) -> str:
  cleaned = net.upper().replace("-", "").replace("_", "").strip()
  if cleaned == "TRC20":
    return "TRC-20"
  if cleaned == "BEP20":
    return "BEP-20"
  if cleaned == "TON":
    return "TON"
  return net.upper().strip()


# === ФУНКЦИЯ ЧЕРЕДОВАНИЯ КРИПТОРЕКВИЗИТОВ ===
def get_next_crypto_wallet(coin: str, network: str):
  now = datetime.datetime.now()
  norm_net = normalize_network(network)
  active = []
  for w in CRYPTO_WALLETS:
    if (
        w["coin"].upper() == coin.upper()
        and normalize_network(w["network"]) == norm_net
    ):
      if w["frozen_until"] and w["frozen_until"] > now:
        continue
      elif w["frozen_until"] and w["frozen_until"] <= now:
        w["frozen_until"] = None
      active.append(w)

  if not active:
    return None

  key = f"{coin.upper()}_{norm_net}"
  if key not in WALLET_POINTERS:
    WALLET_POINTERS[key] = 0

  index = WALLET_POINTERS[key] % len(active)
  chosen = active[index]
  WALLET_POINTERS[key] = (index + 1) % len(active)
  return chosen["address"]


# === ФУНКЦИЯ ПОЛУЧЕНИЯ КУРСОВ ВАЛЮТ ===
async def get_exchange_rate(target_currency: str) -> float:
  url = "https://open.er-api.com/v6/latest/USD"
  try:
    async with ClientSession() as session:
      async with session.get(url, timeout=5) as response:
        if response.status == 200:
          data = await response.json()
          rates = data.get("rates", {})
          if target_currency in rates:
            return float(rates[target_currency])
  except Exception as e:
    logger.warning(f"Не удалось получить курс для {target_currency}: {e}")

  fallback_rates = {
      "RUB": 82.5,
      "BYN": 3.25,
      "KZT": 495.0,
      "UAH": 41.5,
      "UZS": 12800.0,
      "MDL": 18.0,
      "KGS": 87.0,
      "AZN": 1.7,
      "AMD": 390.0,
      "GEL": 2.75,
  }
  return fallback_rates.get(target_currency, 1.0)


def format_local_amount(amount_usd: float, rate: float) -> str:
  val = amount_usd * rate
  return f"{val:,.2f}".replace(",", " ").replace(".", ",")


def build_payment_keyboard() -> InlineKeyboardMarkup:
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🚀 Оплата через xRocket",
                  callback_data="pay_xrocket",
                  style="danger",
              )
          ],
          [
              InlineKeyboardButton(
                  text="💎 Реквизиты (Крипта)",
                  callback_data="crypto_expert",
                  style="primary",
              )
          ],
          [
              InlineKeyboardButton(
                  text="💬 Нужна помощь",
                  callback_data="payment_help_manager",
                  style="primary",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="start_order", style="danger"
              )
          ],
      ]
  )


async def get_payment_context(user_id: int, state: FSMContext):
  data = await state.get_data()
  bill = USER_BILLS.get(user_id, {})
  country = (
      data.get("country")
      or bill.get("country")
      or USER_LAST_COUNTRY.get(user_id, "Россия")
  )
  curr_info = COUNTRY_CURRENCIES.get(country, {"code": "USD", "symbol": "USD"})
  curr_code = data.get("curr_code") or bill.get("curr_code") or curr_info["code"]
  curr_symbol = (
      data.get("curr_symbol") or bill.get("curr_symbol") or curr_info["symbol"]
  )
  if bill:
    total_price = bill.get("total_price", 250)
  else:
    total_price = (
        data.get("total_price") if data.get("total_price") is not None else 250
    )
  service_name = (
      data.get("service_name") or bill.get("service_name") or "Услуга"
  )
  return data, country, curr_code, curr_symbol, total_price, service_name


# === РАСЧЕТ РЕПУТАЦИИ ===
def get_reputation_stats():
  current_date = datetime.date.today()
  base_date = datetime.date(2026, 1, 1)
  days_passed = max(0, (current_date - base_date).days)
  completed = 11000 + sum(
      random.Random(20260101 + d).randint(115, 270) for d in range(days_passed)
  )
  rng = random.Random(current_date.toordinal())
  in_progress = rng.randint(134, 357)
  new_requests = rng.randint(29, 46)
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


# === УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ЦЕЛЬНОГО ОБНОВЛЕНИЯ БАННЕРОВ ===
async def render_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    banner_name: str,
    text: str,
    reply_markup: InlineKeyboardMarkup,
):
  bot = event.bot
  chat_id = (
      event.chat.id if isinstance(event, Message) else event.message.chat.id
  )

  data = await state.get_data()
  bot_msg_id = data.get("bot_msg_id")

  photo_source = None
  banner_path = get_banner_path(banner_name)

  if banner_name in BANNER_CACHE:
    photo_source = BANNER_CACHE[banner_name]
  elif banner_path and os.path.exists(banner_path):
    photo_source = FSInputFile(banner_path)

  if isinstance(event, CallbackQuery):
    await event.answer()
    msg = event.message
    if photo_source:
      try:
        if msg.photo:
          sent_msg = await msg.edit_media(
              media=InputMediaPhoto(
                  media=photo_source, caption=text, parse_mode="HTML"
              ),
              reply_markup=reply_markup,
          )
        else:
          await msg.delete()
          sent_msg = await bot.send_photo(
              chat_id=chat_id,
              photo=photo_source,
              caption=text,
              reply_markup=reply_markup,
              parse_mode="HTML",
          )

        if (
            banner_name not in BANNER_CACHE
            and hasattr(sent_msg, "photo")
            and sent_msg.photo
        ):
          BANNER_CACHE[banner_name] = sent_msg.photo[-1].file_id
      except Exception:
        try:
          await msg.delete()
        except Exception:
          pass
        new_msg = await bot.send_photo(
            chat_id=chat_id,
            photo=photo_source,
            caption=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        if banner_name not in BANNER_CACHE and new_msg.photo:
          BANNER_CACHE[banner_name] = new_msg.photo[-1].file_id
        await state.update_data(bot_msg_id=new_msg.message_id)
        return
    else:
      try:
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
      except Exception:
        try:
          new_msg = await bot.send_message(
              chat_id=chat_id,
              text=text,
              reply_markup=reply_markup,
              parse_mode="HTML",
          )
          await state.update_data(bot_msg_id=new_msg.message_id)
        except Exception:
          pass
        return
    await state.update_data(bot_msg_id=msg.message_id)
  else:
    try:
      await event.delete()
    except Exception:
      pass

    if bot_msg_id:
      try:
        if photo_source:
          sent_msg = await bot.edit_message_media(
              chat_id=chat_id,
              message_id=bot_msg_id,
              media=InputMediaPhoto(
                  media=photo_source, caption=text, parse_mode="HTML"
              ),
              reply_markup=reply_markup,
          )
          if (
              banner_name not in BANNER_CACHE
              and hasattr(sent_msg, "photo")
              and sent_msg.photo
          ):
            BANNER_CACHE[banner_name] = sent_msg.photo[-1].file_id
        else:
          await bot.edit_message_text(
              chat_id=chat_id,
              message_id=bot_msg_id,
              text=text,
              reply_markup=reply_markup,
              parse_mode="HTML",
          )
        return
      except Exception:
        pass

    if photo_source:
      new_msg = await bot.send_photo(
          chat_id=chat_id,
          photo=photo_source,
          caption=text,
          reply_markup=reply_markup,
          parse_mode="HTML",
      )
      if banner_name not in BANNER_CACHE and new_msg.photo:
        BANNER_CACHE[banner_name] = new_msg.photo[-1].file_id
    else:
      new_msg = await bot.send_message(
          chat_id=chat_id,
          text=text,
          reply_markup=reply_markup,
          parse_mode="HTML",
      )
    await state.update_data(bot_msg_id=new_msg.message_id)


# === ГЛАВНОЕ МЕНЮ ===
async def show_main_menu(event: Message | CallbackQuery, state: FSMContext):
  await state.clear()
  user_id = event.from_user.id
  if user_id not in KNOWN_USERS:
    KNOWN_USERS[user_id] = datetime.datetime.now()

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🛒 Заказать", callback_data="start_order", style="danger"
              )
          ],
          [
              InlineKeyboardButton(
                  text="📦 Мои заказы",
                  callback_data="my_orders",
                  style="primary",
              ),
              InlineKeyboardButton(
                  text="📊 Репутация",
                  callback_data="reputation",
                  style="primary",
              ),
          ],
          [
              InlineKeyboardButton(
                  text="💬 Поддержка",
                  callback_data="contact_admin",
                  style="danger",
              )
          ],
      ]
  )
  text = (
      "🏆 <b>ARENA SHOP — СПОРТ</b>\n\n"
      "<i>Добро пожаловать в сервис. Все конфиденциально, быстро и безопасно."
      " Работаем 24/7.</i>\n\n"
      "<b>👇 Выберите пункт меню ниже:</b>"
  )
  await render_step(event, state, "main.jpg", text, keyboard)


# === АДМИНСКИЕ И МЕНЕДЖЕРСКИЕ КОМАНДЫ ===
@router.message(Command("ban"))
async def cmd_ban_user(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply(
        "❌ Использование: `/ban <user_id>`", parse_mode="Markdown"
    )
    return
  target = args[1].strip()
  if target.isdigit():
    user_id = int(target)
    BANNED_USERS.add(user_id)
    await message.reply(
        f"✅ Пользователь с ID <code>{user_id}</code> заблокирован.",
        parse_mode="HTML",
    )
  else:
    await message.reply(
        "❌ Укажите корректный числовой ID.", parse_mode="Markdown"
    )


@router.message(Command("bill"))
async def cmd_bill_user(message: Message, state: FSMContext):
  allowed_staff = {uid for uid in (ADMIN_ID, MANAGER_ID) if uid is not None}
  if allowed_staff and message.from_user.id not in allowed_staff:
    return

  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply(
        "❌ Использование: `/bill <user_id> <услуга> <сумма$>`\nПример: `/bill"
        " 282810030 особый заказ 220$`",
        parse_mode="Markdown",
    )
    return

  parts = args[1].strip().split()
  if len(parts) < 2 or not parts[0].isdigit():
    await message.reply(
        "❌ Укажите корректный числовой `user_id`, название услуги и сумму.\nПример:"
        " `/bill 282810030 особый заказ 220$`",
        parse_mode="Markdown",
    )
    return

  target_user_id = int(parts[0])
  raw_amount = (
      parts[-1]
      .replace("$", "")
      .replace("usd", "")
      .replace("USD", "")
      .replace(",", ".")
      .strip()
  )
  try:
    amount_usd = float(raw_amount)
    if amount_usd <= 0:
      raise ValueError()
  except ValueError:
    await message.reply(
        "❌ Некорректная сумма. Пример: `/bill 282810030 особый заказ 220$`",
        parse_mode="Markdown",
    )
    return

  service_name = " ".join(parts[1:-1]).strip() if len(parts) > 2 else "Особый заказ"
  if not service_name:
    service_name = "Особый заказ"

  amount_val = (
      int(amount_usd) if amount_usd.is_integer() else round(amount_usd, 2)
  )
  amount_str = str(amount_val)
  country = USER_LAST_COUNTRY.get(target_user_id, "Россия")
  curr_info = COUNTRY_CURRENCIES.get(country, {"code": "USD", "symbol": "USD"})
  rate = await get_exchange_rate(curr_info["code"])
  local_amount_str = format_local_amount(amount_usd, rate)

  USER_BILLS[target_user_id] = {
      "service_name": service_name,
      "total_price": amount_val,
      "country": country,
      "curr_code": curr_info["code"],
      "curr_symbol": curr_info["symbol"],
  }
  if message.from_user.id == target_user_id:
    await state.update_data(
        service_name=service_name,
        total_price=amount_val,
        country=country,
        curr_code=curr_info["code"],
        curr_symbol=curr_info["symbol"],
    )

  bill_text = (
      "⏳ <b>Оплата заказа</b>\n"
      "Время на завершение: <b><u>20:00</u></b>\n\n"
      f"🧾 <b>Счет на {service_name}</b>\n"
      f"💳 <b>Способ оплаты ({country})</b>\n"
      f"К оплате: <b>{amount_str} USDT</b> <i>({local_amount_str}"
      f" {curr_info['symbol']})</i>"
  )

  if service_name.lower() != "особый заказ":
    if target_user_id not in USER_ORDERS:
      USER_ORDERS[target_user_id] = []
    USER_ORDERS[target_user_id].append({
        "service_name": service_name,
        "total_price": amount_val,
        "status": "🧾 Выставлен счет",
    })

  try:
    banner_name = "payment.jpg"
    banner_path = get_banner_path(banner_name)
    if banner_name in BANNER_CACHE:
      sent_bill = await message.bot.send_photo(
          chat_id=target_user_id,
          photo=BANNER_CACHE[banner_name],
          caption=bill_text,
          reply_markup=build_payment_keyboard(),
          parse_mode="HTML",
      )
    elif banner_path and os.path.exists(banner_path):
      sent_bill = await message.bot.send_photo(
          chat_id=target_user_id,
          photo=FSInputFile(banner_path),
          caption=bill_text,
          reply_markup=build_payment_keyboard(),
          parse_mode="HTML",
      )
      if sent_bill.photo:
        BANNER_CACHE[banner_name] = sent_bill.photo[-1].file_id
    else:
      sent_bill = await message.bot.send_message(
          chat_id=target_user_id,
          text=bill_text,
          reply_markup=build_payment_keyboard(),
          parse_mode="HTML",
      )
    USER_BILLS[target_user_id]["bill_msg_id"] = sent_bill.message_id
    if message.from_user.id == target_user_id:
      await state.update_data(bot_msg_id=sent_bill.message_id)
    await message.reply(
        f"✅ Счет на <b>{service_name}</b> (<b>{amount_str} USDT</b>)"
        f" отправлен пользователю <code>{target_user_id}</code> (страна:"
        f" <b>{country}</b>, валюта: <b>{curr_info['symbol']}</b>).",
        parse_mode="HTML",
    )
  except Exception as e:
    await message.reply(
        f"❌ Не удалось отправить счет пользователю"
        f" <code>{target_user_id}</code>: {e}",
        parse_mode="HTML",
    )


@router.message(Command("send"))
async def cmd_send_to_user(message: Message):
  allowed_staff = {uid for uid in (ADMIN_ID, MANAGER_ID) if uid is not None}
  if allowed_staff and message.from_user.id not in allowed_staff:
    return
  args = message.text.split(maxsplit=2)
  if len(args) < 3:
    await message.reply(
        "❌ Использование: `/send <user_id> <текст сообщения>`",
        parse_mode="Markdown",
    )
    return
  target_id_str = args[1].strip()
  text_to_send = args[2].strip()
  if target_id_str.isdigit():
    target_user_id = int(target_id_str)
    try:
      await message.bot.send_message(
          chat_id=target_user_id,
          text=f"💬 <b>Сообщение от менеджера:</b>\n\n{text_to_send}",
          parse_mode="HTML",
      )
      await message.reply(
          f"✅ Сообщение отправлено пользователю <code>{target_user_id}</code>.",
          parse_mode="HTML",
      )
    except Exception as e:
      await message.reply(f"❌ Не удалось отправить сообщение: {e}")
  else:
    await message.reply(
        "❌ Укажите корректный числовой ID пользователя.", parse_mode="Markdown"
    )


@router.message(F.reply_to_message, F.text)
async def admin_reply_handler(message: Message):
  allowed_staff = {uid for uid in (ADMIN_ID, MANAGER_ID) if uid is not None}
  if allowed_staff and message.from_user.id not in allowed_staff:
    return

  reply_msg = message.reply_to_message
  text_source = (reply_msg.text or "") + "\n" + (reply_msg.caption or "")

  match = re.search(r"ID:\s*<code>(\d+)</code>|<code>(\d+)</code>", text_source)
  if not match:
    match = re.search(r"(?:ID:|\b)(\d{6,})", text_source)

  if match:
    target_user_id = int(match.group(1) or match.group(2))
    text_lower = text_source.lower()
    is_special = "особый заказ" in text_lower or "заказ" in text_lower
    prefix = (
        "💬 <b>Ответ от менеджера по вашему заказу:</b>"
        if is_special
        else "💬 <b>Ответ от службы поддержки:</b>"
    )

    try:
      await message.bot.send_message(
          chat_id=target_user_id,
          text=f"{prefix}\n\n{message.text}",
          parse_mode="HTML",
      )
      await message.reply("✅ Ответ успешно отправлен клиенту.")
    except Exception as e:
      await message.reply(f"❌ Не удалось отправить сообщение клиенту: {e}")


@router.message(Command("clear_cache"))
async def cmd_clear_cache(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  BANNER_CACHE.clear()
  await message.reply("✅ Кеш баннеров успешно очищен.")


@router.message(Command("add"))
async def cmd_add_wallet(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply(
        "❌ Использование: `/add TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe USDT"
        " TRC-20`",
        parse_mode="Markdown",
    )
    return
  parts = args[1].split()
  if len(parts) < 3:
    await message.reply(
        "❌ Неверный формат. Пример: `/add TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe"
        " USDT TRC-20`",
        parse_mode="Markdown",
    )
    return
  network = normalize_network(parts[-1])
  coin = parts[-2].upper()
  address = " ".join(parts[:-2]).strip()
  CRYPTO_WALLETS.append({
      "address": address,
      "coin": coin,
      "network": network,
      "frozen_until": None,
  })
  await message.reply(
      f"✅ Криптореквизиты добавлены!\n🪙 {coin} | 🌐"
      f" {network}\n<code>{address}</code>",
      parse_mode="HTML",
  )


@router.message(Command("delete"))
async def cmd_delete_wallet(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  global CRYPTO_WALLETS
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply(
        "❌ Использование: `/delete <кошелек> <монета> <сеть>`",
        parse_mode="Markdown",
    )
    return
  parts = args[1].split()
  if len(parts) < 3:
    await message.reply("❌ Неверный формат.", parse_mode="Markdown")
    return
  network = normalize_network(parts[-1])
  coin = parts[-2].upper()
  address = " ".join(parts[:-2]).strip()
  initial_len = len(CRYPTO_WALLETS)
  CRYPTO_WALLETS = [
      w
      for w in CRYPTO_WALLETS
      if not (
          w["address"] == address
          and w["coin"].upper() == coin
          and normalize_network(w["network"]) == network
      )
  ]
  if len(CRYPTO_WALLETS) < initial_len:
    await message.reply("✅ Кошелек удален.")
  else:
    await message.reply("❌ Кошелек не найден.")


@router.message(Command("freeze"))
async def cmd_freeze_wallet(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply(
        "❌ Использование: `/freeze <кошелек> <монета> <сеть>`",
        parse_mode="Markdown",
    )
    return
  parts = args[1].split()
  if len(parts) < 3:
    await message.reply("❌ Неверный формат.", parse_mode="Markdown")
    return
  network = normalize_network(parts[-1])
  coin = parts[-2].upper()
  address = " ".join(parts[:-2]).strip()
  found = False
  now = datetime.datetime.now()
  for w in CRYPTO_WALLETS:
    if (
        w["address"] == address
        and w["coin"].upper() == coin
        and normalize_network(w["network"]) == network
    ):
      w["frozen_until"] = now + datetime.timedelta(hours=24)
      found = True
      break
  if found:
    await message.reply("✅ Кошелек зафрижен на 24 часа.")
  else:
    await message.reply("❌ Кошелек не найден.")


# === XROCKET PAY API И ВЕБХУК ===
XROCKET_API_URL = "https://pay.api.xrocket.exchange"
XROCKET_LEGACY_URL = "https://pay.xrocket.tg"


def _extract_xrocket_link_and_id(payload: object) -> tuple[str, str]:
  """Находит ссылку на оплату и ID инвойса в любом уровне ответа xRocket."""
  link_keys = (
      "paymentLink",
      "invoiceLink",
      "payLink",
      "payUrl",
      "checkoutUrl",
      "botLink",
      "link",
      "url",
  )
  id_keys = ("invoiceId", "id", "uuid")

  found_link = ""
  found_id = ""

  def walk(obj):
    nonlocal found_link, found_id
    if isinstance(obj, dict):
      for k in link_keys:
        val = obj.get(k)
        if (
            isinstance(val, str)
            and val.strip().startswith(("http://", "https://", "tg://"))
            and not found_link
        ):
          found_link = val.strip()
      for k in id_keys:
        val = obj.get(k)
        if val is not None and str(val).strip() and not found_id:
          found_id = str(val).strip()
      for v in obj.values():
        if isinstance(v, (dict, list)):
          walk(v)
    elif isinstance(obj, list):
      for item in obj:
        if isinstance(item, (dict, list)):
          walk(item)

  walk(payload)
  return found_link, found_id


async def create_xrocket_invoice(
    amount_usd: float, description: str, user_id: int | None = None
):
  if not XROCKET_TOKEN:
    logger.error("XROCKET_TOKEN не задан в переменных окружения!")
    return "", None, "Не задана переменная XROCKET_TOKEN"

  clean_amount = (
      int(amount_usd)
      if float(amount_usd).is_integer()
      else round(float(amount_usd), 2)
  )
  amount_str = str(clean_amount)
  webhook_url = (
      f"{WEBHOOK_BASE_URL}/xrocket/webhook" if WEBHOOK_BASE_URL else None
  )

  v1_payload = {
      "priceAmount": amount_str,
      "priceCurrency": "USDT",
      "payCurrencies": ["USDT"],
      "description": description[:100],
  }
  if webhook_url:
    v1_payload["callbackUrl"] = webhook_url
  if user_id:
    v1_payload["clientInvoiceId"] = (
        f"u{user_id}_{int(datetime.datetime.now().timestamp())}"
    )

  bearer_headers = {
      "Authorization": f"Bearer {XROCKET_TOKEN}",
      "Content-Type": "application/json",
      "Accept": "application/json",
  }

  last_error = "Не удалось получить ссылку на оплату от xRocket"
  auth_rejected = False

  async with ClientSession() as session:
    try:
      url_v1 = f"{XROCKET_API_URL}/api/v1/invoices"
      async with session.post(
          url_v1, json=v1_payload, headers=bearer_headers, timeout=10
      ) as resp:
        resp_text = await resp.text()
        logger.info(f"xRocket POST {url_v1} -> {resp.status}: {resp_text}")
        if resp.status in (200, 201):
          try:
            data = await resp.json()
          except Exception:
            data = {}
          pay_link, inv_id = _extract_xrocket_link_and_id(data)
          if not pay_link and inv_id:
            pay_link = f"https://t.me/xrocket?start=inv_{inv_id}"
          if pay_link:
            if inv_id and user_id:
              INVOICE_TO_USER[inv_id] = user_id
            logger.info(
                f"xRocket invoice created: id={inv_id}, pay_url={pay_link}"
            )
            return pay_link, inv_id or None, ""
          last_error = (
              data.get("detail")
              or data.get("message")
              or f"API вернул статус {resp.status} без ссылки на оплату"
          )
        else:
          try:
            err_json = await resp.json()
            detail = (
                err_json.get("detail")
                or err_json.get("message")
                or err_json.get("title")
                or f"HTTP {resp.status}"
            )
            if "unknown api key" in str(detail).lower() or resp.status in (
                401,
                403,
            ):
              auth_rejected = True
              last_error = (
                  f"{detail} (проверьте Pay API токен в настройках xRocket Pay)"
              )
            else:
              last_error = f"{detail} (HTTP {resp.status})"
          except Exception:
            if resp.status in (401, 403):
              auth_rejected = True
            last_error = f"HTTP {resp.status}"
    except Exception as e:
      logger.warning(
          f"Ошибка запроса xRocket ({XROCKET_API_URL}/api/v1/invoices): {e}"
      )
      last_error = f"{type(e).__name__}: {e}"

    if not auth_rejected:
      try:
        url_legacy = f"{XROCKET_LEGACY_URL}/tg-invoices"
        legacy_headers = {
            "Rocket-Pay-Key": XROCKET_TOKEN,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        legacy_payload = {
            "amount": clean_amount,
            "numPayments": 1,
            "currency": "USDT",
            "description": description[:100],
            "payload": f"user:{user_id}" if user_id else "order_payment",
            "expiredIn": 1200,
        }
        async with session.post(
            url_legacy, json=legacy_payload, headers=legacy_headers, timeout=10
        ) as resp:
          resp_text = await resp.text()
          logger.info(
              f"xRocket Legacy POST {url_legacy} -> {resp.status}: {resp_text}"
          )
          if resp.status in (200, 201):
            try:
              data = await resp.json()
            except Exception:
              data = {}
            pay_link, inv_id = _extract_xrocket_link_and_id(data)
            if not pay_link and inv_id:
              pay_link = f"https://t.me/xrocket?start=inv_{inv_id}"
            if pay_link:
              if inv_id and user_id:
                INVOICE_TO_USER[inv_id] = user_id
              return pay_link, inv_id or None, ""
      except Exception as e:
        logger.warning(f"Ошибка резервного запроса xRocket Legacy: {e}")

  return "", None, str(last_error)[:160]


async def check_xrocket_invoice_status(invoice_id) -> bool:
  if not invoice_id:
    return False
  inv_str = str(invoice_id)
  if inv_str in PAID_INVOICES:
    return True
  if not XROCKET_TOKEN:
    return False

  bearer_headers = {
      "Authorization": f"Bearer {XROCKET_TOKEN}",
      "Accept": "application/json",
  }

  async with ClientSession() as session:
    for endpoint, params in [
        (f"{XROCKET_API_URL}/api/v1/invoice/{inv_str}", None),
        (f"{XROCKET_API_URL}/api/v1/invoice", {"invoiceId": inv_str}),
    ]:
      try:
        async with session.get(
            endpoint, params=params, headers=bearer_headers, timeout=10
        ) as resp:
          if resp.status == 200:
            data = await resp.json()
            inv_data = (
                data.get("data") if isinstance(data.get("data"), dict) else data
            )
            status = str(inv_data.get("status", "")).lower()
            if status in ("paid", "completed", "active_paid"):
              PAID_INVOICES.add(inv_str)
              return True
      except Exception as e:
        logger.warning(
            f"Ошибка проверки инвойса xRocket Pay API ({endpoint}): {e}"
        )

    try:
      url_legacy = f"{XROCKET_LEGACY_URL}/tg-invoices/{inv_str}"
      legacy_headers = {
          "Rocket-Pay-Key": XROCKET_TOKEN,
          "Accept": "application/json",
      }
      async with session.get(
          url_legacy, headers=legacy_headers, timeout=10
      ) as resp:
        if resp.status == 200:
          data = await resp.json()
          inv_data = (
              data.get("data") if isinstance(data.get("data"), dict) else data
          )
          status = str(inv_data.get("status", "")).lower()
          if status in ("paid", "completed"):
            PAID_INVOICES.add(inv_str)
            return True
    except Exception as e:
      logger.warning(f"Ошибка проверки инвойса xRocket Legacy: {e}")

  return False


async def handle_xrocket_webhook(request: web.Request):
  try:
    payload = await request.json()
  except Exception:
    return web.Response(status=400, text="Invalid JSON")

  logger.info(f"xRocket webhook received: {payload}")
  data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
  _, extracted_id = _extract_xrocket_link_and_id(payload)
  inv_id = str(
      data.get("id")
      or data.get("invoiceId")
      or payload.get("id")
      or extracted_id
      or ""
  )
  status = str(data.get("status") or payload.get("status") or "").lower()
  event_type = str(
      payload.get("type") or payload.get("event") or data.get("event") or ""
  ).lower()

  if status in ("paid", "completed") or "paid" in event_type:
    if inv_id and inv_id not in PAID_INVOICES:
      PAID_INVOICES.add(inv_id)
      user_id = INVOICE_TO_USER.get(inv_id)
      if user_id and BOT_INSTANCE:
        task = ABANDONED_CHECKOUTS.pop(user_id, None)
        if task:
          task.cancel()
        bill = USER_BILLS.pop(user_id, {})
        service_desc = bill.get("service_name") or "Услуга"
        total_price = bill.get("total_price") or 0
        if user_id not in USER_ORDERS:
          USER_ORDERS[user_id] = []
        USER_ORDERS[user_id].append({
            "service_name": service_desc,
            "total_price": total_price,
            "status": "✅ Оплачено через xRocket (вебхук)",
        })
        try:
          await BOT_INSTANCE.send_message(
              chat_id=user_id,
              text=(
                  "✅ <b>Оплата подтверждена через вебхук xRocket!</b>\n\nВаша"
                  " заявка принята в работу."
              ),
              parse_mode="HTML",
          )
        except Exception:
          pass
        if ADMIN_ID:
          try:
            await BOT_INSTANCE.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"🚀 Оплата xRocket (вебхук) от ID <code>{user_id}</code>"
                    f" (счет <code>{inv_id}</code>)\n• Услуга: {service_desc}"
                ),
                parse_mode="HTML",
            )
          except Exception:
            pass

  return web.Response(status=200, text="OK")


# === ФОНОВЫЕ ЗАДАЧИ ===
async def scheduled_broadcast(bot: Bot):
  while True:
    await asyncio.sleep(3600)
    now = datetime.datetime.now()
    banner_path = get_banner_path("special_offer.jpg")
    for user_id, last_time in list(KNOWN_USERS.items()):
      if last_time is None or (now - last_time).total_seconds() >= 24 * 3600:
        try:
          today = datetime.date.today()
          target_date = today + datetime.timedelta(days=3)
          months_ru = [
              "",
              "января",
              "февраля",
              "марта",
              "апреля",
              "мая",
              "июня",
              "июля",
              "августа",
              "сентября",
              "октября",
              "ноября",
              "декабря",
          ]
          text = (
              "✨ <b>Специальное предложение</b>\n\n"
              "Действует персональная скидка <b>20%</b> до"
              f" <b>{target_date.day} {months_ru[target_date.month]}</b>.\n\n"
              "Ждем вас в боте 👇"
          )
          keyboard = InlineKeyboardMarkup(
              inline_keyboard=[
                  [
                      InlineKeyboardButton(
                          text="✨ Оформить со скидкой",
                          callback_data="start_order",
                          style="danger",
                      )
                  ],
                  [
                      InlineKeyboardButton(
                          text="🏠 На главную",
                          callback_data="back_home",
                          style="primary",
                      )
                  ],
              ]
          )

          if "special_offer.jpg" in BANNER_CACHE:
            photo = BANNER_CACHE["special_offer.jpg"]
            await bot.send_photo(
                chat_id=user_id,
                photo=photo,
                caption=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
          elif banner_path and os.path.exists(banner_path):
            photo = FSInputFile(banner_path)
            sent = await bot.send_photo(
                chat_id=user_id,
                photo=photo,
                caption=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            if sent.photo:
              BANNER_CACHE["special_offer.jpg"] = sent.photo[-1].file_id
          else:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )

          KNOWN_USERS[user_id] = now
        except Exception as e:
          logger.error(f"Ошибка рассылки: {e}")


async def delayed_abandoned_cart_reminder(bot: Bot, user_id: int):
  try:
    await asyncio.sleep(1800)
    if user_id in ABANDONED_CHECKOUTS:
      reminder_text = (
          "👋 <b>Завершить заказ со скидкой -20%?</b>\n\nВы остановились на"
          " этапе оплаты. Возвращайтесь к оформлению 👇"
      )
      keyboard = InlineKeyboardMarkup(
          inline_keyboard=[
              [
                  InlineKeyboardButton(
                      text="🛒 Вернуться к заказу",
                      callback_data="start_order",
                      style="danger",
                  )
              ],
              [
                  InlineKeyboardButton(
                      text="💬 Поддержка",
                      callback_data="contact_admin",
                      style="danger",
                  )
              ],
          ]
      )

      banner_path = get_banner_path("abandoned_cart.jpg")
      if "abandoned_cart.jpg" in BANNER_CACHE:
        photo = BANNER_CACHE["abandoned_cart.jpg"]
        await bot.send_photo(
            chat_id=user_id,
            photo=photo,
            caption=reminder_text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
      elif banner_path and os.path.exists(banner_path):
        photo = FSInputFile(banner_path)
        sent = await bot.send_photo(
            chat_id=user_id,
            photo=photo,
            caption=reminder_text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        if sent.photo:
          BANNER_CACHE["abandoned_cart.jpg"] = sent.photo[-1].file_id
      else:
        await bot.send_message(
            chat_id=user_id,
            text=reminder_text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

      ABANDONED_CHECKOUTS.pop(user_id, None)
  except asyncio.CancelledError:
    pass


def build_catalog_keyboard() -> InlineKeyboardMarkup:
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🧎 Извинение на коленях — 50$",
                  callback_data="srv_Извинение_на_коленях_50",
                  style="primary",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🥊 Избить — 100$",
                  callback_data="srv_Избить_100",
                  style="danger",
              )
          ],
          [
              InlineKeyboardButton(
                  text="💸 Вернуть долг — 150$",
                  callback_data="srv_Вернуть_долг_150",
                  style="primary",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🦴 Сломать пальцы — 200$",
                  callback_data="srv_Сломать_пальцы_200",
                  style="danger",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🦾 Сломать конечности — 300$",
                  callback_data="srv_Сломать_конечности_300",
                  style="primary",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🪓 Отрубить конечности — 500$",
                  callback_data="srv_Отрубить_конечности_500",
                  style="danger",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⚡️ Убийство — 4000$",
                  callback_data="srv_Убийство_4000",
                  style="primary",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🎯 Особый заказ — Рассчитать сумму",
                  callback_data="srv_Особый_заказ_0",
                  style="danger",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="back_home", style="primary"
              )
          ],
      ]
  )


# === ОБРАБОТЧИКИ КОМАНД ===
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
  await show_main_menu(message, state)


@router.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext):
  await message.reply("🔄 Бот перезапущен, состояние очищено.")
  await show_main_menu(message, state)


@router.message(Command("order"))
async def cmd_order(message: Message, state: FSMContext):
  user_id = message.from_user.id
  if user_id not in KNOWN_USERS:
    KNOWN_USERS[user_id] = datetime.datetime.now()
  await state.set_state(OrderForm.service)
  text = "🛒 <b>Каталог услуг</b>\n\nВыберите подходящий вариант из списка ниже:"
  await render_step(message, state, "catalog.jpg", text, build_catalog_keyboard())


@router.message(Command("my_orders"))
async def cmd_my_orders_msg(message: Message, state: FSMContext):
  user_id = message.from_user.id
  orders = USER_ORDERS.get(user_id, [])
  if not orders:
    text = (
        "📦 <b>Мои заказы</b>\n\nУ вас пока нет активных или завершенных"
        " заказов."
    )
  else:
    text = "📦 <b>История ваших заказов:</b>\n\n"
    for idx, ord_data in enumerate(orders, 1):
      price_str = (
          f"{ord_data['total_price']}$"
          if ord_data["total_price"] > 0
          else "Индивидуально"
      )
      text += (
          f"<b>Заказ #{idx}</b>\n• Услуга: {ord_data['service_name']}\n• Сумма:"
          f" {price_str}\n• Статус: {ord_data['status']}\n\n"
      )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="back_home", style="primary"
              )
          ]
      ]
  )
  await render_step(message, state, "my_orders.jpg", text, keyboard)


@router.message(Command("reputation"))
async def cmd_reputation_msg(message: Message, state: FSMContext):
  completed, in_progress, new_requests = get_reputation_stats()
  text = (
      "📊 <b>Репутация сервиса</b>\n\n"
      f"• Успешно выполнено задач: <b>{completed:,}</b>\n"
      f"• В активной работе: <b>{in_progress}</b>\n"
      f"• Новых заявок сегодня: <b>{new_requests}</b>\n\n"
      "<i>Конфиденциальность и безопасность гарантированы.</i>"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="back_home", style="primary"
              )
          ]
      ]
  )
  await render_step(message, state, "reputation.jpg", text, keyboard)


@router.callback_query(F.data == "back_home")
async def back_home(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  task = ABANDONED_CHECKOUTS.pop(user_id, None)
  if task:
    task.cancel()
  await show_main_menu(callback, state)


@router.callback_query(F.data == "reputation")
async def show_reputation(callback: CallbackQuery, state: FSMContext):
  completed, in_progress, new_requests = get_reputation_stats()
  text = (
      "📊 <b>Репутация сервиса</b>\n\n"
      f"• Успешно выполнено задач: <b>{completed:,}</b>\n"
      f"• В активной работе: <b>{in_progress}</b>\n"
      f"• Новых заявок сегодня: <b>{new_requests}</b>\n\n"
      "<i>Конфиденциальность и безопасность гарантированы.</i>"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="back_home", style="primary"
              )
          ]
      ]
  )
  await render_step(callback, state, "reputation.jpg", text, keyboard)


@router.callback_query(F.data == "my_orders")
async def show_my_orders(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  orders = USER_ORDERS.get(user_id, [])
  if not orders:
    text = (
        "📦 <b>Мои заказы</b>\n\nУ вас пока нет активных или завершенных"
        " заказов."
    )
  else:
    text = "📦 <b>История ваших заказов:</b>\n\n"
    for idx, ord_data in enumerate(orders, 1):
      price_str = (
          f"{ord_data['total_price']}$"
          if ord_data["total_price"] > 0
          else "Индивидуально"
      )
      text += (
          f"<b>Заказ #{idx}</b>\n• Услуга: {ord_data['service_name']}\n• Сумма:"
          f" {price_str}\n• Статус: {ord_data['status']}\n\n"
      )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="back_home", style="primary"
              )
          ]
      ]
  )
  await render_step(callback, state, "my_orders.jpg", text, keyboard)


@router.callback_query(F.data == "contact_admin")
async def contact_admin_info(callback: CallbackQuery, state: FSMContext):
  await state.set_state(SupportForm.reason)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⬅️ Отмена", callback_data="back_home", style="danger"
              )
          ]
      ]
  )
  text = (
      "💬 <b>Служба поддержки</b>\n\n"
      "Опишите вашу ситуацию или задайте вопрос (оплата, статус заказа и т.д.):"
  )
  await render_step(callback, state, "support.jpg", text, keyboard)


@router.message(SupportForm.reason, F.text)
async def process_support_reason(message: Message, state: FSMContext):
  await state.update_data(reason=message.text.strip())
  await state.set_state(SupportForm.photo)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⏩ Пропустить",
                  callback_data="skip_support_photo",
                  style="primary",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Отмена", callback_data="back_home", style="danger"
              )
          ],
      ]
  )
  text = "📸 Отправьте фото при необходимости или нажмите пропустить:"
  await render_step(message, state, "support.jpg", text, keyboard)


@router.message(SupportForm.photo, F.photo)
async def process_support_photo(message: Message, state: FSMContext):
  photo_id = message.photo[-1].file_id
  data = await state.get_data()
  user = message.from_user
  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  admin_text = (
      "📩 <b>Новое обращение в поддержку</b>\n\n"
      f"• От кого: {user_info} (ID: <code>{user.id}</code>)\n"
      f"• Вопрос: {data.get('reason')}"
  )
  for staff_id in {uid for uid in (ADMIN_ID, MANAGER_ID) if uid is not None}:
    try:
      await message.bot.send_photo(
          chat_id=staff_id,
          photo=photo_id,
          caption=admin_text,
          parse_mode="HTML",
      )
    except Exception:
      pass
  text = "✅ Обращение отправлено! Специалист скоро свяжется с вами."
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🏠 На главную", callback_data="back_home", style="danger"
              )
          ]
      ]
  )
  await render_step(message, state, "main.jpg", text, keyboard)
  await state.clear()


@router.callback_query(SupportForm.photo, F.data == "skip_support_photo")
async def skip_support_photo(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  user = callback.from_user
  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  admin_text = (
      "📩 <b>Новое обращение в поддержку</b>\n\n"
      f"• От кого: {user_info} (ID: <code>{user.id}</code>)\n"
      f"• Вопрос: {data.get('reason')}"
  )
  for staff_id in {uid for uid in (ADMIN_ID, MANAGER_ID) if uid is not None}:
    try:
      await callback.bot.send_message(
          chat_id=staff_id, text=admin_text, parse_mode="HTML"
      )
    except Exception:
      pass
  text = "✅ Обращение отправлено! Специалист скоро свяжется с вами."
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🏠 На главную", callback_data="back_home", style="danger"
              )
          ]
      ]
  )
  await render_step(callback, state, "main.jpg", text, keyboard)
  await state.clear()


# === КАТАЛОГ И ОФОРМЛЕНИЕ ЗАКАЗА ===
@router.callback_query(F.data == "start_order")
async def process_start_order(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  task = ABANDONED_CHECKOUTS.pop(user_id, None)
  if task:
    task.cancel()

  await state.set_state(OrderForm.service)
  text = "🛒 <b>Каталог услуг</b>\n\nВыберите подходящий вариант из списка ниже:"
  await render_step(callback, state, "catalog.jpg", text, build_catalog_keyboard())


@router.callback_query(OrderForm.service, F.data.startswith("srv_"))
async def process_service(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  price = int(parts[-1])
  service_name = " ".join(parts[1:-1]).replace("_", " ")

  await state.update_data(service_name=service_name, service_price=price)

  if service_name == "Вернуть долг":
    await state.set_state(OrderForm.debt_info)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="start_order", style="primary"
                )
            ]
        ]
    )
    text = (
        "💸 <b>Вернуть долг</b>\n\n"
        "Укажите информацию о долге: какая сумма долга, данные должника и т.д.:"
    )
    await render_step(callback, state, "catalog.jpg", text, keyboard)
  elif service_name == "Особый заказ":
    await state.set_state(OrderForm.special_task)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="start_order", style="primary"
                )
            ]
        ]
    )
    text = (
        "🎯 <b>Особый заказ</b>\n\n"
        "Опишите вашу задачу, что нужно будет сделать (с вами свяжется менеджер"
        " для расчета стоимости):"
    )
    await render_step(callback, state, "catalog.jpg", text, keyboard)
  else:
    await state.set_state(OrderForm.country)
    await show_country_selection(callback, state, service_name, price)


async def show_country_selection(
    event: Message | CallbackQuery,
    state: FSMContext,
    service_name: str,
    price: int,
):
  countries = [
      ("🇷🇺 Россия", "country_Russia"),
      ("🇧🇾 Беларусь", "country_Belarus"),
      ("🇺🇦 Украина", "country_Ukraine"),
      ("🇰🇿 Казахстан", "country_Kazakhstan"),
      ("🇺🇿 Узбекистан", "country_Uzbekistan"),
      ("🇲🇩 Молдова", "country_Moldova"),
      ("🇰🇬 Кыргызстан", "country_Kyrgyzstan"),
      ("🇦🇿 Азербайджан", "country_Azerbaijan"),
      ("🇦🇲 Армения", "country_Armenia"),
      ("🇬🇪 Грузия", "country_Georgia"),
  ]
  kb_rows = []
  for i in range(0, len(countries), 2):
    row = [
        InlineKeyboardButton(
            text=countries[i][0], callback_data=countries[i][1], style="primary"
        )
    ]
    if i + 1 < len(countries):
      row.append(
          InlineKeyboardButton(
              text=countries[i + 1][0],
              callback_data=countries[i + 1][1],
              style="danger",
          )
      )
    kb_rows.append(row)
  kb_rows.append(
      [
          InlineKeyboardButton(
              text="⬅️ Назад", callback_data="start_order", style="primary"
          )
      ]
  )

  price_str = f"{price}$" if price > 0 else "Расчет менеджера"
  text = (
      f"🎯 Выбрано: <b>{service_name}</b> ({price_str})\n\n🌍 <b>Страна"
      " вашей цели</b>\n\nВыберите страну из списка:"
  )
  await render_step(
      event,
      state,
      "country.jpg",
      text,
      InlineKeyboardMarkup(inline_keyboard=kb_rows),
  )


@router.message(OrderForm.debt_info, F.text)
async def process_debt_info(message: Message, state: FSMContext):
  await state.update_data(debt_info=message.text.strip())
  await state.set_state(OrderForm.debt_wallet)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="start_order", style="primary"
              )
          ]
      ]
  )
  text = (
      "💸 <b>Вернуть долг</b>\n\n"
      "Теперь укажите свой криптокошелек <b>USDT (TRC-20 / BEP-20)</b> или"
      " <b>TON</b>, чтобы получить назад деньги с должника:"
  )
  await render_step(message, state, "catalog.jpg", text, keyboard)


@router.message(OrderForm.debt_wallet, F.text)
async def process_debt_wallet(message: Message, state: FSMContext):
  await state.update_data(debt_wallet=message.text.strip())
  await state.set_state(OrderForm.country)
  data = await state.get_data()
  await show_country_selection(
      message, state, data.get("service_name"), data.get("service_price")
  )


@router.message(OrderForm.special_task, F.text)
async def process_special_task(message: Message, state: FSMContext):
  await state.update_data(special_task=message.text.strip())
  await state.set_state(OrderForm.country)
  data = await state.get_data()
  await show_country_selection(
      message, state, data.get("service_name"), data.get("service_price")
  )


@router.callback_query(OrderForm.country, F.data.startswith("country_"))
async def process_country(callback: CallbackQuery, state: FSMContext):
  c_code = callback.data.split("_")[1]
  names = {
      "Russia": "Россия",
      "Belarus": "Беларусь",
      "Ukraine": "Украина",
      "Kazakhstan": "Казахстан",
      "Uzbekistan": "Узбекистан",
      "Moldova": "Молдова",
      "Kyrgyzstan": "Кыргызстан",
      "Azerbaijan": "Азербайджан",
      "Armenia": "Армения",
      "Georgia": "Грузия",
  }
  selected = names.get(c_code, c_code)
  USER_LAST_COUNTRY[callback.from_user.id] = selected
  curr_info = COUNTRY_CURRENCIES.get(selected, {"code": "USD", "symbol": "USD"})
  await state.update_data(
      country=selected,
      curr_code=curr_info["code"],
      curr_symbol=curr_info["symbol"],
  )

  await state.set_state(OrderForm.target_location)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="start_order", style="primary"
              )
          ]
      ]
  )
  text = (
      f"📍 Страна: <b>{selected}</b>\n\n"
      f"🏢 <b>Локация цели</b>\n"
      f"Укажите город, адрес, дом или квартиру следующим сообщением:"
  )
  await render_step(callback, state, "location.jpg", text, keyboard)


@router.message(OrderForm.target_location, F.text)
async def process_target_location(message: Message, state: FSMContext):
  await state.update_data(target_location=message.text.strip())
  await state.set_state(OrderForm.target_data)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="start_order", style="primary"
              )
          ]
      ]
  )
  text = (
      "🪪 <b>Данные цели</b>\n\n"
      "Укажите ФИО или другую информацию о цели (если неизвестно, напишите"
      " «неизвестно»):"
  )
  await render_step(message, state, "target_data.jpg", text, keyboard)


@router.message(OrderForm.target_data, F.text)
async def process_target_data(message: Message, state: FSMContext):
  await state.update_data(target_data=message.text.strip(), photos=[])
  await state.set_state(OrderForm.photos)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⏩ Пропустить",
                  callback_data="finish_photos",
                  style="primary",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="start_order", style="danger"
              )
          ],
      ]
  )
  text = (
      "📸 Прикрепите фото цели (до 3 шт.) или нажмите кнопку пропуска:"
  )
  await render_step(message, state, "photos.jpg", text, keyboard)


@router.message(OrderForm.photos, F.photo)
async def process_photo(message: Message, state: FSMContext):
  await asyncio.sleep(0.3)

  data = await state.get_data()
  photos = data.get("photos", [])
  if len(photos) >= 3:
    await message.answer("⚠️ Добавлено максимальное количество фото (3 шт.).")
    return

  photo_id = message.photo[-1].file_id
  if photo_id not in photos:
    photos.append(photo_id)
    await state.update_data(photos=photos)

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text=f"✅ Готово ({len(photos)}/3)",
                  callback_data="finish_photos",
                  style="primary",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="start_order", style="danger"
              )
          ],
      ]
  )
  text = (
      f"📸 Фото #{len(photos)} успешно добавлено. Можете прикрепить еще или"
      " завершить:"
  )
  await render_step(message, state, "photos.jpg", text, keyboard)


@router.callback_query(OrderForm.photos, F.data == "finish_photos")
async def finish_photos(callback: CallbackQuery, state: FSMContext):
  await state.set_state(OrderForm.urgency)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⚡ За 2-3 часа (+30$)",
                  callback_data="urg_fast_30",
                  style="primary",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🕐 Завтра (+10$)",
                  callback_data="urg_tomorrow_10",
                  style="danger",
              )
          ],
          [
              InlineKeyboardButton(
                  text="📅 Стандартно (2-5 дней)",
                  callback_data="urg_standard_0",
                  style="primary",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="start_order", style="danger"
              )
          ],
      ]
  )
  text = "⏱ <b>Срочность выполнения</b>\n\nВыберите желаемый вариант:"
  await render_step(callback, state, "urgency.jpg", text, keyboard)


@router.callback_query(OrderForm.urgency, F.data.startswith("urg_"))
async def process_urgency(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  urg_price = int(parts[2])
  urg_map = {
      "fast": "⚡ За 2-3 часа",
      "tomorrow": "🕐 Завтра",
      "standard": "📅 2-5 дней",
  }
  urgency = urg_map.get(parts[1], "📅 2-5 дней")

  await state.update_data(urgency=urgency, urgency_price=urg_price)
  data = await state.get_data()

  base_service_price = data.get("service_price", 0)
  total_price = (base_service_price + urg_price) if base_service_price > 0 else 0
  await state.update_data(total_price=total_price)

  rate = await get_exchange_rate(data.get("curr_code", "USD"))
  local_amount_str = (
      format_local_amount(total_price, rate) if total_price > 0 else "0"
  )

  price_display = (
      f"<b>{total_price} USDT</b> <i>({local_amount_str}"
      f" {data.get('curr_symbol')})</i>"
      if total_price > 0
      else "<b>Рассчитывается менеджером</b>"
  )

  is_special = data.get("service_name", "").lower() == "особый заказ"
  payment_btn_text = (
      "✅ Отправить заявку" if is_special else "💳 Перейти к оплате"
  )

  summary_text = (
      "✨ <b>Сводка заказа:</b>\n\n"
      f"• Услуга: <b>{data.get('service_name')}</b>\n"
      f"• Срочность: {urgency}\n"
      f"• Страна: {data.get('country')}\n\n"
      f"💵 <b>Сумма к оплате: {price_display}</b>"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text=payment_btn_text,
                  callback_data="go_to_payment",
                  style="danger",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="start_order", style="primary"
              )
          ],
      ]
  )
  await render_step(callback, state, "summary.jpg", summary_text, keyboard)


@router.callback_query(F.data == "go_to_payment")
async def process_go_to_payment(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  data, country, curr_code, curr_symbol, total_price, service_name = (
      await get_payment_context(user_id, state)
  )

  if service_name.lower() == "особый заказ" and user_id not in USER_BILLS:
    if user_id in ABANDONED_CHECKOUTS:
      task = ABANDONED_CHECKOUTS.pop(user_id, None)
      if task:
        task.cancel()

    user = callback.from_user
    extra_info = f"\n• Задача: {data.get('special_task', 'Не указано')}"
    if data.get("country"):
      extra_info += f"\n• Страна: {data.get('country')}"
    if data.get("target_location"):
      extra_info += f"\n• Локация: {data.get('target_location')}"
    if data.get("target_data"):
      extra_info += f"\n• Данные цели: {data.get('target_data')}"
    if data.get("urgency"):
      extra_info += f"\n• Срочность: {data.get('urgency')}"
    for staff_id in {uid for uid in (ADMIN_ID, MANAGER_ID) if uid is not None}:
      try:
        await callback.bot.send_message(
            chat_id=staff_id,
            text=(
                f"🎯 <b>Новый особый заказ от @{user.username} (ID:"
                f" <code>{user.id}</code>)</b>{extra_info}"
            ),
            parse_mode="HTML",
        )
      except Exception:
        pass

    text = (
        "✅ <b>Заявка принята!</b>\n\nМенеджер свяжется с вами в течение 25"
        " минут и рассчитает стоимость."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏠 На главную",
                    callback_data="back_home",
                    style="danger",
                )
            ]
        ]
    )
    await render_step(callback, state, "main.jpg", text, keyboard)
    await state.clear()
    return

  if user_id in ABANDONED_CHECKOUTS:
    task = ABANDONED_CHECKOUTS.pop(user_id, None)
    if task:
      task.cancel()
  ABANDONED_CHECKOUTS[user_id] = asyncio.create_task(
      delayed_abandoned_cart_reminder(callback.bot, user_id)
  )

  rate = await get_exchange_rate(curr_code)
  local_amount_str = (
      format_local_amount(total_price, rate) if total_price > 0 else "0"
  )

  bill_prefix = (
      f"🧾 <b>Счет на {service_name}</b>\n" if user_id in USER_BILLS else ""
  )
  price_line = (
      f"К оплате: <b>{total_price} USDT</b> <i>({local_amount_str}"
      f" {curr_symbol})</i>"
      if total_price > 0
      else "К оплате: <b>Индивидуальный расчет</b>"
  )

  payment_text = (
      "⏳ <b>Оплата заказа</b>\n"
      "Время на завершение: <b><u>20:00</u></b>\n\n"
      f"{bill_prefix}"
      f"💳 <b>Способ оплаты ({country})</b>\n"
      f"{price_line}"
  )
  keyboard = build_payment_keyboard()

  await render_step(callback, state, "payment.jpg", payment_text, keyboard)


@router.callback_query(F.data == "payment_help_manager")
async def process_payment_help_manager(callback: CallbackQuery):
  user = callback.from_user
  for staff_id in {uid for uid in (ADMIN_ID, MANAGER_ID) if uid is not None}:
    try:
      await callback.bot.send_message(
          chat_id=staff_id,
          text=(
              f"🚨 Запрос помощи с оплатой от @{user.username} (ID:"
              f" <code>{user.id}</code>)"
          ),
          parse_mode="HTML",
      )
    except Exception:
      pass
  await callback.answer(
      "✅ Менеджер уведомлен, скоро свяжется с вами.", show_alert=True
  )


@router.callback_query(F.data == "pay_xrocket")
async def process_pay_xrocket(callback: CallbackQuery, state: FSMContext):
  _, _, curr_code, curr_symbol, total_price, service_name = (
      await get_payment_context(callback.from_user.id, state)
  )
  if total_price <= 0:
    total_price = 100

  description = (
      f"Оплата: {service_name}" if service_name else "Оплата услуг"
  )
  pay_url, inv_id, err_msg = await create_xrocket_invoice(
      total_price, description, user_id=callback.from_user.id
  )
  if not pay_url:
    await callback.answer(f"❌ Ошибка xRocket: {err_msg}", show_alert=True)
    return

  rate = await get_exchange_rate(curr_code)
  local_amount_str = format_local_amount(total_price, rate)

  await state.update_data(invoice_id=inv_id, total_price=total_price)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🔗 Оплатить счет", url=pay_url, style="primary"
              )
          ],
          [
              InlineKeyboardButton(
                  text="✅ Проверить оплату",
                  callback_data="xrocket_check_success",
                  style="danger",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="go_to_payment", style="primary"
              )
          ],
      ]
  )
  text = (
      f"🚀 <b>Оплата через xRocket</b>\n\n"
      f"К оплате: <b>{total_price} USDT</b> <i>({local_amount_str}"
      f" {curr_symbol})</i>\n\n"
      "После оплаты нажмите кнопку проверки:"
  )
  await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data == "xrocket_check_success")
async def process_xrocket_check(callback: CallbackQuery, state: FSMContext):
  data, _, _, _, total_price, service_desc = await get_payment_context(
      callback.from_user.id, state
  )
  inv_id = data.get("invoice_id")

  if not inv_id:
    await callback.answer(
        "⚠️ Счет не найден. Нажмите «Оплата через xRocket» заново.",
        show_alert=True,
    )
    return

  is_paid = await check_xrocket_invoice_status(inv_id)
  if not is_paid:
    await callback.answer(
        "⏳ Платеж еще не поступил. Попробуйте через минуту.", show_alert=True
    )
    return

  user = callback.from_user
  task = ABANDONED_CHECKOUTS.pop(user.id, None)
  if task:
    task.cancel()

  if user.id not in USER_ORDERS:
    USER_ORDERS[user.id] = []

  USER_ORDERS[user.id].append({
      "service_name": service_desc,
      "total_price": total_price,
      "status": "✅ Оплачено через xRocket",
  })
  USER_BILLS.pop(user.id, None)

  if ADMIN_ID:
    try:
      extra_info = ""
      if data.get("debt_info"):
        extra_info += f"\n• Информация о долге: {data.get('debt_info')}"
      if data.get("debt_wallet"):
        extra_info += (
            f"\n• Кошелек для возврата (USDT/TON): {data.get('debt_wallet')}"
        )
      if data.get("special_task"):
        extra_info += f"\n• Задача (Особый заказ): {data.get('special_task')}"
      await callback.bot.send_message(
          chat_id=ADMIN_ID,
          text=(
              f"🚀 Успешная оплата xRocket от @{user.username} (ID:"
              f" <code>{user.id}</code>)\n• Услуга: {service_desc}{extra_info}"
          ),
          parse_mode="HTML",
      )
    except Exception:
      pass

  text = "✅ <b>Оплата подтверждена!</b>\n\nВаша заявка принята в работу."
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🏠 На главную", callback_data="back_home", style="danger"
              )
          ]
      ]
  )
  await render_step(callback, state, "main.jpg", text, keyboard)
  await state.clear()


@router.callback_query(F.data == "crypto_expert")
async def process_crypto_expert(callback: CallbackQuery, state: FSMContext):
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="💎 USDT (TRC-20)",
                  callback_data="pay_crypto_trc20",
                  style="primary",
              )
          ],
          [
              InlineKeyboardButton(
                  text="💵 USDT (BEP-20)",
                  callback_data="pay_crypto_bep20",
                  style="danger",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🪙 TON", callback_data="pay_crypto_ton", style="primary"
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="go_to_payment", style="primary"
              )
          ],
      ]
  )
  text = "💎 Выберите криптовалютную сеть для перевода:"
  await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data.startswith("pay_crypto_"))
async def process_pay_crypto(callback: CallbackQuery, state: FSMContext):
  net_key = callback.data.split("_")[2]
  net_info = CRYPTO_NETWORKS.get(net_key)
  if not net_info:
    await callback.answer("Сеть не найдена", show_alert=True)
    return

  wallet_address = get_next_crypto_wallet(
      net_info["coin"], net_info["network"]
  )
  if not wallet_address:
    text = (
        "⏳ <b>Реквизиты временно недоступны</b>\n\n"
        f"По сети <b>{net_info['name']}</b> сейчас обновляем реквизиты — мы"
        " уже над этим работаем. Через 5 минут можете вернуться и оплатить,"
        " либо выбрать другой способ оплаты ниже 👇"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Оплата через xRocket",
                    callback_data="pay_xrocket",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💎 Другая сеть",
                    callback_data="crypto_expert",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="go_to_payment", style="primary"
                )
            ],
        ]
    )
    await render_step(callback, state, "payment.jpg", text, keyboard)
    return

  _, _, curr_code, curr_symbol, total_price, _ = await get_payment_context(
      callback.from_user.id, state
  )
  rate = await get_exchange_rate(curr_code)
  local_amount_str = (
      format_local_amount(total_price, rate) if total_price > 0 else "0"
  )

  price_str = (
      f"<b>{total_price} USDT</b> <i>({local_amount_str} {curr_symbol})</i>"
      if total_price > 0
      else "<b>Индивидуальный расчет</b>"
  )

  text = (
      f"💳 <b>Оплата {net_info['name']}</b>\n\n"
      f"• К оплате: {price_str}\n"
      f"• Сеть: <b>{net_info['network']}</b>\n"
      f"• Реквизиты:\n<code>{wallet_address}</code>\n\n"
      "💡 <i>После перевода прикрепите чек.</i>"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Прикрепить чек",
                  callback_data="ask_for_receipt",
                  style="danger",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="crypto_expert", style="primary"
              )
          ],
      ]
  )
  await render_step(callback, state, "payment.jpg", text, keyboard)


@router.callback_query(F.data == "ask_for_receipt")
async def ask_for_receipt(callback: CallbackQuery, state: FSMContext):
  await state.set_state(OrderForm.receipt)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="go_to_payment", style="primary"
              )
          ]
      ]
  )
  text = "🧾 Отправьте скриншот или фото чека следующим сообщением:"
  await render_step(callback, state, "payment.jpg", text, keyboard)


@router.message(OrderForm.receipt, F.photo)
async def process_receipt_photo(message: Message, state: FSMContext):
  user = message.from_user
  task = ABANDONED_CHECKOUTS.pop(user.id, None)
  if task:
    task.cancel()
  data, _, _, _, total_price, service_desc = await get_payment_context(
      user.id, state
  )

  if user.id not in USER_ORDERS:
    USER_ORDERS[user.id] = []

  USER_ORDERS[user.id].append({
      "service_name": service_desc,
      "total_price": total_price,
      "status": "⏳ Ожидает проверки чека",
  })
  USER_BILLS.pop(user.id, None)

  if ADMIN_ID:
    try:
      extra_info = ""
      if data.get("debt_info"):
        extra_info += f"\n• Информация о долге: {data.get('debt_info')}"
      if data.get("debt_wallet"):
        extra_info += (
            f"\n• Кошелек для возврата (USDT/TON): {data.get('debt_wallet')}"
        )
      if data.get("special_task"):
        extra_info += f"\n• Задача (Особый заказ): {data.get('special_task')}"
      price_val = (
          f"{total_price} USDT" if total_price > 0 else "Расчет менеджера"
      )
      await message.bot.send_photo(
          chat_id=ADMIN_ID,
          photo=message.photo[-1].file_id,
          caption=(
              f"🛒 Новый чек от @{user.username} (ID:"
              f" <code>{user.id}</code>)\n• Услуга: {service_desc}\n• Сумма:"
              f" {price_val}{extra_info}"
          ),
          parse_mode="HTML",
      )
    except Exception:
      pass

  text = "✅ Чек получен! Менеджер проверит платеж в течение 25 минут."
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🏠 На главную", callback_data="back_home", style="danger"
              )
          ]
      ]
  )
  await render_step(message, state, "main.jpg", text, keyboard)
  await state.clear()


# === ЗАЩИТНЫЕ (FALLBACK) ХЕНДЛЕРЫ ОТ ЗАВИСАНИЙ И НЕВЕРНОГО ВВОДА ===
@router.message(OrderForm.debt_info)
async def fallback_order_debt_info(message: Message):
  await message.reply(
      "⚠️ Пожалуйста, отправьте текстовое сообщение с информацией о долге и"
      " сумме."
  )


@router.message(OrderForm.debt_wallet)
async def fallback_order_debt_wallet(message: Message):
  await message.reply(
      "⚠️ Пожалуйста, отправьте текстовое сообщение с вашим криптокошельком"
      " USDT или TON."
  )


@router.message(OrderForm.special_task)
async def fallback_order_special_task(message: Message):
  await message.reply(
      "⚠️ Пожалуйста, опишите вашу задачу текстовым сообщением."
  )


@router.message(OrderForm.target_location)
async def fallback_order_location(message: Message):
  await message.reply(
      "⚠️ Пожалуйста, отправьте текстовое сообщение с локацией цели (город,"
      " адрес, дом или квартира)."
  )


@router.message(OrderForm.target_data)
async def fallback_order_data(message: Message):
  await message.reply(
      "⚠️ Пожалуйста, отправьте текстовое сообщение с данными цели (ФИО или"
      " «неизвестно»)."
  )


@router.message(OrderForm.photos)
async def fallback_order_photos(message: Message):
  await message.reply(
      "⚠️ Пожалуйста, отправьте фото цели или нажмите кнопку «Готово» /"
      " «Пропустить»."
  )


@router.message(OrderForm.receipt)
async def fallback_order_receipt(message: Message):
  await message.reply(
      "⚠️ Пожалуйста, отправьте скриншот или фото чека об оплате."
  )


@router.message(SupportForm.reason)
async def fallback_support_reason(message: Message):
  await message.reply(
      "⚠️ Пожалуйста, опишите вашу ситуацию текстовым сообщением."
  )


@router.message(SupportForm.photo)
async def fallback_support_photo(message: Message):
  await message.reply(
      "⚠️ Пожалуйста, отправьте фото или нажмите кнопку «Пропустить»."
  )


# === WEB SERVER FOR RENDER / RAILWAY ===
async def handle_ping(request):
  return web.Response(text="Bot is running!")


async def start_web_server():
  app = web.Application()
  app.router.add_get("/", handle_ping)
  app.router.add_post("/xrocket/webhook", handle_xrocket_webhook)
  runner = web.AppRunner(app)
  await runner.setup()
  port = int(os.getenv("PORT", 10000))
  site = web.TCPSite(runner, "0.0.0.0", port)
  await site.start()


async def main():
  global BOT_INSTANCE
  bot = Bot(token=BOT_TOKEN)
  BOT_INSTANCE = bot
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
      logger.info("Запуск поллинга бота...")
      await dp.start_polling(bot)
    except Exception as e:
      logger.error(f"Ошибка поллинга: {e}. Переподключение через 5 секунд...")
      await asyncio.sleep(5)


if __name__ == "__main__":
  asyncio.run(main())
