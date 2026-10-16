from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Ensure imports work when running from project root or elsewhere.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tg_push import TelegramPusher  # noqa: E402


def main() -> int:
    load_dotenv(".env")

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        raise ValueError(
            "Please set TELEGRA... in .env first:\n"
            "- TELEGRA..._BOT_TOKEN\n"
            "- TELEGRAM_CHAT_ID (private chat id, e.g. 123456789)"
        )

    # send_interval=0: allow immediate send for test.
    pusher = TelegramPusher(bot_token=bot_token, chat_id=chat_id, send_interval=0)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pusher.push(subject="HelloWorld", content=f"HelloWorld\nsent_at={now}\n")
    print("Done. If network/token/chat_id is correct, check your Telegram inbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

