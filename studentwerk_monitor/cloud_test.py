"""Notification-only GitHub Actions test; never accesses the housing website."""

import os
from datetime import datetime

from .notifications import NotificationError, Notifier
from .schedule import BERLIN

SECRET_FIELDS = {
    "telegram_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "gmail_address": "GMAIL_SENDER",
    "gmail_recipient": "GMAIL_RECIPIENT",
    "gmail_app_password": "GMAIL_APP_PASSWORD",
}


def run_test(environment: dict, *, notifier=None) -> dict:
    if environment.get("GITHUB_ACTIONS") != "true":
        raise ValueError("This command is a cloud proof and must run inside GitHub Actions.")
    missing = [name for name in SECRET_FIELDS.values() if not environment.get(name)]
    if missing:
        raise ValueError("Missing GitHub Actions secrets: " + ", ".join(missing))
    sender = notifier or Notifier({field: environment[name] for field, name in SECRET_FIELDS.items()})
    when = datetime.now(BERLIN).isoformat(timespec="seconds")
    repository = environment.get("GITHUB_REPOSITORY", "")
    run_id = environment.get("GITHUB_RUN_ID", "")
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}"
    body = (
        "CLOUD NOTIFICATION TEST ONLY — this is not a housing offer.\n\n"
        f"Executed on GitHub Actions: {when}\n"
        f"Execution log: {run_url}\n\n"
        "This test runs on GitHub's computer and does not depend on your Mac staying on. "
        "The housing website was not accessed. Housing monitoring remains disabled."
    )
    results = {}
    for channel in ("telegram", "gmail"):
        try:
            sender.send(channel, "Cloud test only — no housing check", body)
        except NotificationError as exc:
            results[channel] = False
            print(f"{channel}: {exc}")
        else:
            results[channel] = True
            print(f"{channel}: accepted by the sending service")
    return results


def main() -> int:
    try:
        results = run_test(dict(os.environ))
    except ValueError as exc:
        print(str(exc))
        return 1
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

