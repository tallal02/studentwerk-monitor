"""A one-time, clearly labelled scheduler test. This never reads housing pages."""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from .config import CHANNELS, DEFAULT_HOME, ensure_home, notification_config, save_json
from .notifications import NotificationError, Notifier
from .schedule import BERLIN, local_now


def run_proof(home: Path, run_id: str, scheduled_at: datetime, *, now: datetime | None = None, notifier=None) -> dict:
    now = (now or local_now()).astimezone(BERLIN)
    ensure_home(home)
    report_path = home / f"scheduler-test-{run_id}.json"
    report = {"scheduled_at": scheduled_at.isoformat(), "executed_at": now.isoformat(), "channels": {}}
    try:
        descriptor = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {"already_executed": True}
    with os.fdopen(descriptor, "w") as stream:
        json.dump({**report, "status": "started"}, stream)
    # A stale calendar trigger must not send a proof on a later day/year.
    if not 0 <= (now - scheduled_at).total_seconds() < 3600:
        report["status"] = "missed-time-window"
        save_json(report_path, report)
        return report
    sender = notifier or Notifier(notification_config(home))
    body = (
        "SCHEDULER TEST ONLY — this is not a housing offer.\n\n"
        f"Scheduled for: {scheduled_at:%Y-%m-%d %H:%M:%S %Z}\n"
        f"Actually executed: {now:%Y-%m-%d %H:%M:%S %Z}\n\n"
        "Your Mac ran this one-time test and sent this notification. "
        "The housing website was not accessed. Housing monitoring remains paused."
    )
    for channel in CHANNELS:
        try:
            sender.send(channel, "Scheduler test only — no housing check", body)
        except NotificationError as exc:
            report["channels"][channel] = {"accepted": False, "error": str(exc)}
        else:
            report["channels"][channel] = {"accepted": True}
        save_json(report_path, {**report, "status": "sending"})
    report["status"] = "finished"
    save_json(report_path, report)
    return report


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scheduled-at", required=True)
    args = parser.parse_args()
    if not args.run_id.isalnum():
        parser.error("run-id must contain only letters and digits")
    scheduled = datetime.fromisoformat(args.scheduled_at)
    if scheduled.tzinfo is None:
        parser.error("scheduled-at must include a time zone")
    print(json.dumps(run_proof(args.home, args.run_id, scheduled), indent=2))


if __name__ == "__main__":
    main()
