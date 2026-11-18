"""Telegram Bot API and Gmail SMTP, without exposing credentials in errors."""

import json
import smtplib
import ssl
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class NotificationError(Exception):
    pass


def telegram_request(token: str, method: str, payload: dict) -> dict | list:
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise NotificationError(f"Telegram returned HTTP {exc.code}; check the bot configuration or try again later.") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise NotificationError("Telegram could not be reached or returned an invalid response.") from None
    if not isinstance(result, dict) or not result.get("ok"):
        raise NotificationError("Telegram rejected the request; check the bot configuration.")
    return result["result"]


class Notifier:
    def __init__(self, config: dict):
        self.config = config

    def send(self, channel: str, title: str, body: str) -> None:
        if channel == "telegram":
            text = f"{title}\n\n{body}"
            if len(text) > 4000:
                text = text[:3990] + "…"
            telegram_request(self.config["telegram_token"], "sendMessage", {
                "chat_id": self.config["telegram_chat_id"],
                "text": text,
                "link_preview_options": {"is_disabled": True},
            })
        elif channel == "gmail":
            message = EmailMessage()
            message["Subject"] = f"[Dortmund housing] {title}"
            message["From"] = self.config["gmail_address"]
            message["To"] = self.config["gmail_recipient"]
            message.set_content(body)
            try:
                with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                    smtp.login(self.config["gmail_address"], self.config["gmail_app_password"])
                    refused = smtp.send_message(message)
                    if refused:
                        raise NotificationError("Gmail did not accept the alert recipient.")
            except smtplib.SMTPAuthenticationError:
                raise NotificationError("Gmail authentication failed; check the Gmail address and app password.") from None
            except (smtplib.SMTPException, OSError):
                raise NotificationError("Gmail could not send the alert; it will be retried.") from None
        else:
            raise NotificationError("Unknown notification channel.")


def deliver_pending(state, notifier: Notifier) -> tuple[int, int]:
    sent = 0
    failed = 0
    for item in state.pending():
        payload = json.loads(item["payload"])
        try:
            notifier.send(item["channel"], payload["title"], payload["body"])
        except NotificationError as exc:
            state.delivery_failed(item["id"], str(exc))
            failed += 1
        else:
            state.delivered(item["id"])
            sent += 1
    return sent, failed

