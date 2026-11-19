"""Scheduled cloud worker. Defaults to a paused state and never solves verification."""

import json
import os
from datetime import datetime
from pathlib import Path

from .app import check_once
from .cloud_browser import CloudBrowserReader
from .cloud_schedule import cloud_due_slot, next_cloud_slot
from .cloud_test import SECRET_FIELDS
from .config import CHANNELS, ConfigurationError
from .notifications import Notifier, deliver_pending
from .schedule import local_now
from .sealed_state import restore_state, seal_state
from .state import State


def config_from_environment(environment: dict) -> dict:
    config = {}
    for field, secret in SECRET_FIELDS.items():
        value = environment.get(secret)
        if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
            raise ConfigurationError(f"GitHub secret {secret} is missing or invalid.")
        config[field] = value
    return config


def run_worker(state: State, mode: str, environment: dict, *, now: datetime | None = None, reader=None, notifier=None) -> dict:
    now = now or local_now()
    if not state.get("cloud_initialized", False):
        with state.db:
            state.put("cloud_initialized", True)
            state.put("access_paused", True)
    if mode not in ("status", "tick", "recover-access"):
        raise ConfigurationError("Unknown cloud operation.")
    common = {"mode": mode, "time": now.isoformat(), "next_planned_slot": next_cloud_slot(now).isoformat()}
    if mode == "status":
        return {**common, "checked": False, "access_paused": state.get("access_paused", True), "summary": state.summary()}
    if mode == "recover-access":
        if environment.get("MANUAL_ACCESS_CONFIRMED") != "true":
            raise ConfigurationError("An explicit confirmation of recovered manual access is required before a cloud validation request.")
        slot = None
    else:
        if environment.get("LIVE_HOUSING_ENABLED") != "true":
            return {**common, "checked": False, "disabled": True}
        slot = cloud_due_slot(now)
        if slot is None:
            return {**common, "checked": False, "outside_window": True}
        if state.get("access_paused", True):
            approval = environment.get("VALIDATION_NOT_BEFORE", "")
            if not approval or state.get("scheduled_validation_used") == approval:
                return {**common, "checked": False, "access_paused": True}
            try:
                approved_at = datetime.fromisoformat(approval)
                if approved_at.tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise ConfigurationError("The scheduled validation time is invalid.") from None
            age = (now - approved_at).total_seconds()
            if age < 0:
                return {**common, "checked": False, "access_paused": True, "waiting_for_validation_time": True}
            if age >= 7200:
                raise ConfigurationError("The one-time cloud validation window was missed. No website request was made.")
            with state.db:
                state.put("scheduled_validation_used", approval)
            mode = "recover-access"
            common["mode"] = "scheduled-validation"
        if not state.claim_slot(slot.isoformat()):
            return {**common, "checked": False, "already_checked": True}
    config = config_from_environment(environment)
    active_reader = reader or CloudBrowserReader(storage_state=state.get("browser_storage"))
    result = check_once(
        state, active_reader, CHANNELS, now=now,
        allow_paused=(mode == "recover-access"), minimum_minutes=120,
    )
    if not result["ok"] and not result.get("deferred"):
        # Fail closed for unexpected content and network errors as well as
        # verification/refusal. No subsequent scheduled fetch is allowed.
        with state.db:
            state.put("access_paused", True)
    if result["ok"] and isinstance(getattr(active_reader, "storage_state", None), dict):
        with state.db:
            state.put("browser_storage", active_reader.storage_state)
    if common["mode"] == "scheduled-validation":
        if result["ok"]:
            body = f"The cloud browser read the listings successfully. Current Dortmund offers: {len(result['dortmund'])}.\n\nThe two-hour schedule is now active on GitHub; your Mac can remain off."
            state.notice(f"activated:{environment['VALIDATION_NOT_BEFORE']}", "Cloud housing monitoring activated", body, CHANNELS)
    if slot is not None:
        outcome = "deferred" if result.get("deferred") else "ok" if result["ok"] else "failed"
        state.finish_slot(slot.isoformat(), outcome, result.get("error"))
    sent, failed = deliver_pending(state, notifier or Notifier(config))
    return {
        **common, **result, "checked": not result.get("deferred", False),
        "access_paused": state.get("access_paused", True), "sent": sent, "failed": failed,
    }


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")


def main() -> int:
    os.umask(0o077)
    environment = dict(os.environ)
    if environment.get("GITHUB_ACTIONS") != "true":
        print("This worker must run in GitHub Actions. No housing request was made.")
        return 1
    home = Path(".cloud-private")
    encrypted = Path(".cloud-state.enc")
    key = environment.get("CLOUD_STATE_KEY", "")
    state = None
    result = None
    error = None
    try:
        restore_state(encrypted, home, key)
        state = State(home)
        result = run_worker(state, environment.get("CLOUD_MODE", "status"), environment)
    except Exception as exc:
        error = str(exc) if isinstance(exc, ConfigurationError) else f"Cloud worker stopped after {type(exc).__name__}; website access remains paused."
        if state is not None:
            with state.db:
                state.put("access_paused", True)
    finally:
        if state is not None:
            state.close()
            try:
                seal_state(home, encrypted, key)
                set_output("state_written", "true")
            except (ConfigurationError, OSError):
                error = "The cloud state could not be saved; scheduled access must remain disabled."
    report = {"error": error, "checked": False} if error else result
    print(json.dumps(report, indent=2, ensure_ascii=False))
    summary = environment.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## Housing monitor status\n\n```json\n" + json.dumps(report, indent=2, ensure_ascii=False) + "\n```\n")
    if error:
        return 1
    return 2 if result.get("ok") is False and not result.get("deferred") else 0


if __name__ == "__main__":
    raise SystemExit(main())

