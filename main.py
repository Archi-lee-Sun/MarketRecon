import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()


from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from graph import app as market_recon_graph

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

TELEGRAM_MAX_LEN = 4096

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def split_for_telegram(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n\n", 0, max_len)
        if split_at == -1:
            split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "გამარჯობა! მომწერეთ პროდუქტი, რომლის ფასების შედარებაც გსურთ."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_query = update.message.text
    chat_id = update.effective_chat.id

    logger.info(f"Received query from {chat_id}: {user_query}")
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    initial_state = {"user_query": user_query}

    try:
        result = await asyncio.to_thread(market_recon_graph.invoke, initial_state)
        final_report = result.get("final_report") or "დაფიქსირდა შეცდომა, სცადეთ თავიდან."
    except Exception as e:
        logger.critical(f"Unhandled error running graph for query '{user_query}': {e}", exc_info=True)
        final_report = "სერვისში მოხდა შეცდომა, სცადეთ მოგვიანებით."

    for chunk in split_for_telegram(final_report):
        await update.message.reply_text(chunk)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.critical(f"Unhandled exception in update {update}: {context.error}", exc_info=context.error)


def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()