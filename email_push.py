from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Iterable


class EmailPusher:
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_pass: str,
        mail_from: str,
        mail_to: Iterable[str],
        use_ssl: bool = True,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_pass = smtp_pass
        self.mail_from = mail_from
        self.mail_to = [x.strip() for x in mail_to if x and x.strip()]
        self.use_ssl = use_ssl

        if not self.mail_to:
            raise ValueError("mail_to cannot be empty")

    def push(self, subject: str, content: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.mail_from
        msg["To"] = ", ".join(self.mail_to)
        msg.set_content(content)

        if self.use_ssl:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30) as server:
                server.login(self.smtp_user, self.smtp_pass)
                server.send_message(msg)
            return

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
            server.starttls()
            server.login(self.smtp_user, self.smtp_pass)
            server.send_message(msg)
