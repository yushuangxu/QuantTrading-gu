from __future__ import annotations

import os
from datetime import datetime

from dotenv import load_dotenv

from email_push import EmailPusher


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def main() -> int:
    load_dotenv(".env")

    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    use_ssl = _env_bool("SMTP_USE_SSL", True)
    mail_from = os.getenv("MAIL_FROM", smtp_user).strip() or smtp_user
    mail_to = [x.strip() for x in os.getenv("MAIL_TO", "").split(",") if x.strip()]

    if not smtp_host or not smtp_user or not smtp_pass or not mail_to:
        raise ValueError("SMTP_HOST/SMTP_USER/SMTP_PASS/MAIL_TO must be configured in .env")

    pusher = EmailPusher(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        mail_from=mail_from,
        mail_to=mail_to,
        use_ssl=use_ssl,
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = "HelloWorld"
    content = f"HelloWorld\n\nsent_at={now}\n"
    pusher.push(subject=subject, content=content)
    print("OK: test email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
