from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from wecom_push import WeComPusher  # noqa: E402


def main() -> int:
    load_dotenv(".env")

    webhook_url = os.getenv("WEWORK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise ValueError("请先在 .env 配好 Wework/企业微信：WEWORK_WEBHOOK_URL")

    # 测试时允许立即发
    pusher = WeComPusher(webhook_url=webhook_url, send_interval=0)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pusher.push(subject="HelloWorld", content=f"HelloWorld\nsent_at={now}\n")
    print("Done. If network/token/webhook is correct, check your 企业微信 群聊天/消息推送。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

