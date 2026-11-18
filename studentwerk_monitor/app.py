"""Commands for setup, inspection, scheduling and local status."""

import argparse
import fcntl
import getpass
import json
import logging
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from .agent import agent_running, install_agent, remove_agent, stop_agent
from .browser import AccessDenied, BraveBrowser, BrowserError, VerificationRequired
from .config import CHANNELS, DEFAULT_HOME, ConfigurationError, config_fingerprint, ensure_home, load_json, notification_config, save_json
from .notifications import NotificationError, Notifier, deliver_pending, telegram_request
from .schedule import BERLIN, due_slot, local_now, next_slot
from .state import State

LOGGER = logging.getLogger("studentwerk")
SCHEDULE_TEXT = "Monday 09:00 through Friday 23:30, Europe/Berlin; hourly on the hour plus Friday 23:30. Dortmund only."


class AlreadyRunning(Exception):
    pass


class CheckDeferred(ConfigurationError):
    def __init__(self, next_allowed_at: float):
        self.next_allowed_at = next_allowed_at
        when = datetime.fromtimestamp(next_allowed_at, BERLIN).strftime("%Y-%m-%d %H:%M %Z")
        super().__init__(f"Live check skipped to prevent closely spaced requests. Earliest next attempt: {when}.")


def read_offers(state: State, browser: BraveBrowser, *, now: datetime | None = None, minimum_minutes: int = 60, allow_paused: bool = False):
    if state.get("access_paused", False) and not allow_paused:
        raise ConfigurationError("Website access is paused. An explicit recovery test is needed after manual access is healthy.")
    allowed, next_allowed_at = state.reserve_navigation((now or local_now()).timestamp(), minimum_minutes)
    if not allowed:
        raise CheckDeferred(next_allowed_at)
    return browser.fetch()


@contextmanager
def run_lock(home: Path):
    ensure_home(home)
    with (home / "monitor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning() from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def configure_logging(home: Path) -> None:
    folder = home / "logs"
    folder.mkdir(exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(folder / "monitor.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)


def local_notification(message: str) -> None:
    script = "display notification " + json.dumps(message, ensure_ascii=False) + ' with title "Dortmund housing monitor"'
    try:
        subprocess.run(["/usr/bin/osascript", "-"], input=script, text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def check_once(state: State, browser: BraveBrowser, channels: tuple[str, ...], *, allow_paused: bool = False, now: datetime | None = None, minimum_minutes: int = 60) -> dict:
    if state.get("access_paused", False) and not allow_paused:
        return {"ok": False, "paused": True, "error": "Website access is paused after a refusal. Recover access manually before using resume-access."}
    try:
        offers = read_offers(state, browser, allow_paused=allow_paused, now=now, minimum_minutes=minimum_minutes)
    except CheckDeferred as exc:
        return {"ok": False, "deferred": True, "next_allowed_at": exc.next_allowed_at, "error": str(exc)}
    except BrowserError as exc:
        kind = "verification" if isinstance(exc, VerificationRequired) else "blocked" if isinstance(exc, AccessDenied) else "browser"
        if isinstance(exc, (AccessDenied, VerificationRequired)):
            with state.db:
                state.put("access_paused", True)
        if state.record_problem(kind, str(exc), channels):
            local_notification(str(exc))
        LOGGER.error("Check failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    baseline, new = state.record_snapshot(offers, channels)
    if allow_paused:
        with state.db:
            state.put("access_paused", False)
    dortmund = [offer for offer in offers if offer.is_dortmund]
    LOGGER.info("Read %d offers; Dortmund=%d; new=%d; baseline=%s", len(offers), len(dortmund), len(new), baseline)
    return {"ok": True, "baseline": baseline, "dortmund": [offer.to_dict() for offer in dortmund], "new": [offer.to_dict() for offer in new]}


def process_tick(state: State, config: dict, browser: BraveBrowser, now: datetime | None = None, notifier=None) -> dict:
    now = now or local_now()
    slot = due_slot(now)
    if slot is None:
        return {"active": False, "checked": False}
    if state.get("access_paused", False):
        sent, failed = deliver_pending(state, notifier or Notifier(config))
        return {"active": True, "checked": False, "paused": True, "sent": sent, "failed": failed}
    result = {"active": True, "checked": False}
    if state.claim_slot(slot.isoformat()):
        minimum_minutes = 30 if slot.weekday() == 4 and slot.hour == 23 and slot.minute == 30 else 60
        result.update(check_once(state, browser, CHANNELS, now=now, minimum_minutes=minimum_minutes))
        result["checked"] = not result.get("deferred", False)
        outcome = "deferred" if result.get("deferred") else "ok" if result["ok"] else "failed"
        state.finish_slot(slot.isoformat(), outcome, result.get("error"))
    sent, failed = deliver_pending(state, notifier or Notifier(config))
    if failed:
        local_notification("An alert could not be delivered. It is queued for retry; check Monitor Status.")
    if result["checked"] or sent or failed:
        LOGGER.info("Scheduled tick: checked=%s sent=%d failed=%d", result["checked"], sent, failed)
    return {**result, "sent": sent, "failed": failed}


def test_alerts(home: Path, state: State) -> None:
    config = notification_config(home)
    notifier = Notifier(config)
    results = {}
    for channel in CHANNELS:
        try:
            notifier.send(channel, "Test alert", "Notification setup test.\n\n" + SCHEDULE_TEXT + "\n\nThis confirms this notification channel works. Website checks remain disabled until separately activated.")
            results[channel] = True
            print(f"  {channel}: test accepted by the sending service.", flush=True)
        except NotificationError as exc:
            results[channel] = False
            print(f"  {channel}: {exc}", flush=True)
    with state.db:
        state.put("notification_tests", {"fingerprint": config_fingerprint(config), "results": results})
    if not all(results.values()):
        raise ConfigurationError("At least one notification test failed. Setup is saved; correct it and rerun setup. Recurring checks were not enabled.")


def require_notification_tests(home: Path, state: State) -> None:
    config = notification_config(home)
    tests = state.get("notification_tests", {})
    if tests.get("fingerprint") != config_fingerprint(config) or not all(tests.get("results", {}).get(channel) for channel in CHANNELS):
        raise ConfigurationError("Both channels need successful tests for the current configuration. Run setup or test-alerts first.")


def require_tested(home: Path, state: State) -> None:
    if state.get("access_paused", False):
        raise ConfigurationError("Website access is paused after an access refusal. Recover access manually before enabling the monitor.")
    require_notification_tests(home, state)
    if not state.get("baselined", False):
        raise ConfigurationError("A successful baseline check is required before enabling recurring checks.")


def _input(label: str, default: str = "") -> str:
    value = input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
    return value or default


def _secret(label: str, old: str = "") -> str:
    suffix = " (press Return to keep the saved value)" if old else ""
    return getpass.getpass(label + suffix + ": ").strip() or old


def setup(home: Path, state: State) -> None:
    print("\nDortmund housing monitor — local notification setup\n", flush=True)
    print(SCHEDULE_TEXT)
    print("Credentials are entered here with hidden input and stored in your private local application folder.")
    print("This setup connects and tests notifications only. It does not access the housing website or enable checks.\n", flush=True)
    if agent_running():
        stop_agent()
        print("Paused the existing monitor while updating setup.", flush=True)
    old_settings = load_json(home / "config.json")
    old_secrets = load_json(home / "credentials.json")
    print("TELEGRAM")
    print("Open https://t.me/BotFather in Telegram, send /newbot, and follow its steps.")
    token = _secret("Bot token", old_secrets.get("telegram_token", ""))
    if not re.fullmatch(r"\d{5,16}:[A-Za-z0-9_-]{20,}", token):
        raise ConfigurationError("That does not look like a Telegram bot token. Copy it from BotFather into this local setup window.")
    identity = telegram_request(token, "getMe", {})
    username = identity.get("username")
    if not username or not re.fullmatch(r"[A-Za-z0-9_]+", username):
        raise ConfigurationError("Could not identify the Telegram bot.")
    print(f"\nOpen https://t.me/{username}, tap Start, then return here.", flush=True)
    input("Press Return after tapping Start: ")
    updates = telegram_request(token, "getUpdates", {"timeout": 0, "limit": 100, "allowed_updates": ["message"]})
    chats = {}
    for update in updates:
        message = update.get("message", {})
        chat = message.get("chat", {})
        if chat.get("type") == "private" and message.get("from", {}).get("id") == chat.get("id"):
            chats[str(chat["id"])] = chat.get("first_name", "Private chat")
    saved_chat = old_settings.get("telegram_chat_id") if token == old_secrets.get("telegram_token") else None
    if not chats and saved_chat:
        chat_id = saved_chat
    elif len(chats) == 1:
        chat_id = next(iter(chats))
    elif len(chats) > 1:
        for chat_id, name in chats.items():
            print(f"  {chat_id}: {name}")
        chat_id = _input("Your private chat ID", saved_chat or "")
        if chat_id not in chats:
            raise ConfigurationError("Choose one of the displayed private chat IDs.")
    else:
        raise ConfigurationError("No private conversation found. Open your bot, tap Start, and run setup again.")
    # Save the successful Telegram step so a later Gmail issue does not require re-entry.
    save_json(home / "config.json", {**old_settings, "telegram_chat_id": str(chat_id)})
    save_json(home / "credentials.json", {**old_secrets, "telegram_token": token})
    print("\nGMAIL")
    print("Use a Gmail app password: https://myaccount.google.com/apppasswords")
    print("Google requires 2-Step Verification for app passwords. Enter the app password here.")
    address = _input("Gmail sending address", old_settings.get("gmail_address", ""))
    if not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", address):
        raise ConfigurationError("Enter a complete email address.")
    recipient = _input("Receive alerts at", old_settings.get("gmail_recipient", address))
    if not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", recipient):
        raise ConfigurationError("Enter a complete recipient email address.")
    password = _secret("Gmail app password", old_secrets.get("gmail_app_password", "")).replace(" ", "")
    if not re.fullmatch(r"[A-Za-z0-9]{16}", password):
        raise ConfigurationError("A Gmail app password should have 16 characters; spaces are ignored.")
    save_json(home / "config.json", {"telegram_chat_id": str(chat_id), "gmail_address": address, "gmail_recipient": recipient})
    save_json(home / "credentials.json", {"telegram_token": token, "gmail_app_password": password})
    print("\nSending one clearly labelled test alert to each channel...", flush=True)
    test_alerts(home, state)
    print("\nTelegram and Gmail setup is complete. Website checks remain disabled.", flush=True)
    print("The website-access pause is unchanged. Activation is a separate step after manual access has recovered.")
    print("Use Monitor Status.command to see notification readiness and the access pause.")


def show_status(home: Path, state: State) -> None:
    now = local_now()
    summary = state.summary()
    loaded = agent_running()
    paused = state.get("access_paused", False)
    summary.update({
        "schedule": SCHEDULE_TEXT,
        "timezone": "Europe/Berlin",
        "now": now.isoformat(),
        "next_scheduled_check": next_slot(now).isoformat() if loaded and not paused else None,
        "agent_loaded": loaded,
        "data_folder": str(home),
        "website_access_paused": paused,
    })
    if summary.get("last_success"):
        summary["last_success"] = datetime.fromtimestamp(summary["last_success"], BERLIN).isoformat()
    last_navigation = state.get("last_navigation_at")
    summary["last_navigation"] = datetime.fromtimestamp(last_navigation, BERLIN).isoformat() if last_navigation else None
    try:
        require_notification_tests(home, state)
        summary["notifications_ready"] = True
    except ConfigurationError:
        summary["notifications_ready"] = False
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Dortmund housing monitor for your local Brave browser.")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME, help="Private data directory (default: user Library/Application Support).")
    parser.add_argument("command", choices=("inspect", "baseline", "check", "tick", "status", "setup", "test-alerts", "install", "stop", "resume-access"))
    parser.add_argument("--manual-access-confirmed", action="store_true", help="For resume-access only: confirm a manual visit has recovered before one controlled test.")
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    ensure_home(home)
    configure_logging(home)
    state = State(home)
    try:
        if args.command == "inspect":
            if state.get("access_paused", False):
                raise ConfigurationError("Website access is paused. Check the site manually after waiting; resume-access is for an explicit recovery test.")
            with run_lock(home):
                offers = read_offers(state, BraveBrowser())
            print(json.dumps({"total_offers": len(offers), "dortmund": [offer.to_dict() for offer in offers if offer.is_dortmund]}, ensure_ascii=False, indent=2))
        elif args.command == "status":
            show_status(home, state)
        elif args.command == "setup":
            setup(home, state)
        elif args.command == "test-alerts":
            test_alerts(home, state)
        elif args.command == "install":
            require_tested(home, state)
            print(f"Enabled: {install_agent(home)}")
        elif args.command == "stop":
            remove_agent()
            print("Recurring checks stopped. Local settings and listing history were kept.")
        elif args.command == "resume-access":
            if not args.manual_access_confirmed:
                raise ConfigurationError("Housing access remains paused. Only after a successful manual visit, use resume-access --manual-access-confirmed for one controlled test.")
            with run_lock(home):
                try:
                    require_notification_tests(home, state)
                    channels = CHANNELS
                except ConfigurationError:
                    channels = ()
                result = check_once(state, BraveBrowser(), channels, allow_paused=True)
                if not result["ok"]:
                    with state.db:
                        state.put("access_paused", True)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["ok"] else 1
        else:
            with run_lock(home):
                if args.command == "tick":
                    # No browser or credential access outside the agreed window.
                    if due_slot(local_now()) is None:
                        return 0
                    config = notification_config(home)
                    process_tick(state, config, BraveBrowser())
                else:
                    config = notification_config(home) if args.command == "check" else None
                    if args.command == "baseline" and state.get("baselined", False):
                        raise ConfigurationError("A baseline already exists. Use inspect for a read-only check, or check to detect new offers.")
                    result = check_once(state, BraveBrowser(), CHANNELS if config else ())
                    if config:
                        result["sent"], result["failed"] = deliver_pending(state, Notifier(config))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 0 if result["ok"] else 1
    except AlreadyRunning:
        print("Another monitor check is already running.")
        return 0
    except (ConfigurationError, BrowserError, NotificationError) as exc:
        if isinstance(exc, (AccessDenied, VerificationRequired)):
            with state.db:
                state.put("access_paused", True)
            state.record_problem("blocked" if isinstance(exc, AccessDenied) else "verification", str(exc), ())
        print(str(exc), file=sys.stderr)
        LOGGER.error("%s", exc)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nSetup stopped. Recurring checks were not enabled.", file=sys.stderr)
        return 1
    finally:
        state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

