import logging
import os
from pathlib import Path

import requests
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
IP_FILE = Path(os.getenv("IP_FILE", "data/public_ip.txt"))
CHAT_FILE = Path(os.getenv("CHAT_FILE", "data/chat_id.txt"))
USERS_FILE = Path(os.getenv("USERS_FILE", "data/allowed_users.txt"))
LOG_FILE = Path(os.getenv("LOG_FILE", "data/bot.log"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
IP_SERVICE_URL = os.getenv("IP_SERVICE_URL", "https://api.ipify.org?format=json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DELETE_AFTER_SECONDS = int(os.getenv("DELETE_AFTER_SECONDS", "60"))

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("ip-bot")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(content), encoding="utf-8")


def load_allowed_users(path: Path) -> set[int]:
    users: set[int] = set()
    if not path.exists():
        return users

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            users.add(int(line))
        except ValueError:
            logger.warning("Invalid user_id in %s: %s", path, line)
    return users


def get_public_ip() -> str:
    r = requests.get(IP_SERVICE_URL, timeout=10)
    r.raise_for_status()
    payload = r.json()
    ip = payload.get("ip", "").strip()
    if not ip:
        raise RuntimeError("Empty IP response")
    return ip


def get_saved_chat_id() -> int | None:
    raw = read_text(CHAT_FILE)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid chat_id in %s", CHAT_FILE)
        return None


def save_chat_id(chat_id: int) -> None:
    write_text(CHAT_FILE, chat_id)


def read_last_ip() -> str:
    return read_text(IP_FILE)


def write_last_ip(ip: str) -> None:
    write_text(IP_FILE, ip)


def is_allowed_user(user_id: int | None, allowed_users: set[int]) -> bool:
    return user_id is not None and user_id in allowed_users


# async def auto_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
#     await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
async def auto_delete(context: ContextTypes.DEFAULT_TYPE):
    """Background job to delete the message."""
    data = context.job.data
    chat_id = data["chat_id"]
    message_id = data["message_id"]
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(f"Failed to delete message {message_id}: {e}")


async def send_reply_and_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    msg = await update.message.reply_text(text)
    context.job_queue.run_once(
        auto_delete,
        when=DELETE_AFTER_SECONDS,
        data={"chat_id": msg.chat_id, "message_id": msg.message_id},
    )


async def deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_reply_and_delete(update, context, "Non autorizzato.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_users: set[int] = context.application.bot_data["allowed_users"]
    user_id = update.effective_user.id if update.effective_user else None

    if not is_allowed_user(user_id, allowed_users):
        await deny(update, context)
        return

    chat = update.effective_chat
    if chat:
        save_chat_id(chat.id)

    try:
        ip = read_last_ip() or get_public_ip()
        if not read_last_ip():
            write_last_ip(ip)
    except Exception as e:
        logger.exception("Failed on /start")
        ip = f"errore: {e}"

    text = (
        f"Bot attivo.\n"
        f"Chat ID salvato: {chat.id if chat else 'n/d'}\n"
        f"IP corrente: {ip}\n"
        f"Usa /status.\n"
        f"Comandi disponibili: /start, /status, /reload_users"
    )
    await send_reply_and_delete(update, context, text)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_users: set[int] = context.application.bot_data["allowed_users"]
    user_id = update.effective_user.id if update.effective_user else None

    if not is_allowed_user(user_id, allowed_users):
        await deny(update, context)
        return

    chat = update.effective_chat
    if chat:
        save_chat_id(chat.id)

    try:
        ip = get_public_ip()
        write_last_ip(ip)
        await send_reply_and_delete(update, context, f"IP pubblico corrente: {ip}")
    except Exception as e:
        logger.exception("Failed on /status")
        await send_reply_and_delete(update, context, f"Errore nel recupero dell'IP: {e}")


async def reload_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_users: set[int] = context.application.bot_data["allowed_users"]
    user_id = update.effective_user.id if update.effective_user else None

    if not is_allowed_user(user_id, allowed_users):
        await deny(update, context)
        return

    new_users = load_allowed_users(USERS_FILE)
    context.application.bot_data["allowed_users"] = new_users
    logger.info("Whitelist reloaded: %d users", len(new_users))
    await send_reply_and_delete(update, context, f"Whitelist ricaricata. Utenti autorizzati: {len(new_users)}")


async def monitor_ip(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        current_ip = get_public_ip()
    except Exception as e:
        logger.warning("Failed to fetch public IP: %s", e)
        return

    last_ip = read_last_ip()

    if not last_ip:
        write_last_ip(current_ip)
        logger.info("Initial IP saved: %s", current_ip)
        return

    if current_ip != last_ip:
        write_last_ip(current_ip)
        chat_id = get_saved_chat_id()
        logger.info("IP changed from %s to %s", last_ip, current_ip)

        if chat_id is not None:
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"IP pubblico cambiato: {current_ip}")
            except Exception as e:
                logger.exception("Failed to send notification: %s", e)


async def post_init(app: Application) -> None:
    allowed_users = load_allowed_users(USERS_FILE)
    if not allowed_users:
        raise RuntimeError(f"Nessun utente autorizzato trovato in {USERS_FILE}")

    app.bot_data["allowed_users"] = allowed_users
    logger.info("Loaded %d allowed users", len(allowed_users))

    await app.bot.set_my_commands([
        BotCommand("start", "Avvia il bot"),
        BotCommand("status", "Mostra l'IP pubblico corrente"),
        BotCommand("reload_users", "Ricarica la whitelist utenti"),
    ])

    try:
        initial_ip = get_public_ip()
        write_last_ip(initial_ip)
        logger.info("Initial IP saved at startup: %s", initial_ip)
    except Exception as e:
        logger.exception("Unable to save initial IP at startup: %s", e)

    app.job_queue.run_repeating(monitor_ip, interval=CHECK_INTERVAL, first=CHECK_INTERVAL)
    logger.info("IP monitor scheduled every %s seconds", CHECK_INTERVAL)


def main() -> None:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN non impostato")

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("reload_users", reload_users))

    logger.info("Bot starting")
    application.run_polling()


if __name__ == "__main__":
    main()