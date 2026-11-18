"""Durable listing history and independent notification delivery queues."""

import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from .browser import Offer
from .config import ensure_home


class State:
    def __init__(self, home: Path):
        ensure_home(home)
        path = home / "state.db"
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        path.chmod(0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checks (
                slot TEXT PRIMARY KEY, started_at REAL NOT NULL,
                finished_at REAL, outcome TEXT NOT NULL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS offers (
                id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
                details TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY, event_key TEXT NOT NULL,
                channel TEXT NOT NULL, payload TEXT NOT NULL,
                created_at REAL NOT NULL, sent_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0, last_error TEXT,
                UNIQUE(event_key, channel)
            );
        """)

    def close(self) -> None:
        self.db.close()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))

    def reserve_navigation(self, now: float, minimum_minutes: int = 60) -> tuple[bool, float]:
        """Reserve one attempt across all commands, even if it fails or crashes.

        Minute precision matches the calendar schedule and avoids a few seconds
        of scheduler jitter incorrectly suppressing the next hourly check.
        """
        minute = int(now // 60)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.get("last_navigation_minute")
            if previous is not None and minute < previous + minimum_minutes:
                return False, (previous + minimum_minutes) * 60
            self.put("last_navigation_minute", minute)
            self.put("last_navigation_at", now)
        return True, (minute + minimum_minutes) * 60

    def claim_slot(self, slot: str) -> bool:
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO checks(slot, started_at, outcome) VALUES (?, ?, 'running')",
                (slot, time.time()),
            )
        return cursor.rowcount == 1

    def finish_slot(self, slot: str, outcome: str, error: str | None = None) -> None:
        with self.db:
            self.db.execute("UPDATE checks SET finished_at=?, outcome=?, error=? WHERE slot=?", (time.time(), outcome, error, slot))

    def _enqueue(self, event_key: str, title: str, body: str, channels: tuple[str, ...]) -> None:
        payload = json.dumps({"title": title, "body": body}, ensure_ascii=False)
        for channel in channels:
            self.db.execute(
                "INSERT OR IGNORE INTO deliveries(event_key, channel, payload, created_at) VALUES (?, ?, ?, ?)",
                (event_key, channel, payload, time.time()),
            )

    def record_snapshot(self, offers: list[Offer], channels: tuple[str, ...]) -> tuple[bool, list[Offer]]:
        """The first successful snapshot is a baseline. Invalid pages never reach here."""
        dortmund = [offer for offer in offers if offer.is_dortmund]
        baseline = not self.get("baselined", False)
        new = []
        now = time.time()
        with self.db:
            for offer in dortmund:
                exists = self.db.execute("SELECT 1 FROM offers WHERE id=?", (offer.id,)).fetchone()
                if not exists:
                    self.db.execute("INSERT INTO offers VALUES (?, ?, ?, ?, ?, ?)", (offer.id, offer.url, offer.title, offer.details, now, now))
                    if not baseline:
                        new.append(offer)
                        body = f"{offer.title}\n{offer.details}\n\nApply / details:\n{offer.url}"
                        self._enqueue(f"offer:{offer.id}", "New Dortmund housing offer", body, channels)
                else:
                    self.db.execute("UPDATE offers SET url=?, title=?, details=?, last_seen=? WHERE id=?", (offer.url, offer.title, offer.details, now, offer.id))
            self.put("baselined", True)
            self.put("last_success", now)
            self.put("current_dortmund_count", len(dortmund))
            self.put("last_snapshot_count", len(offers))
            if self.get("problem"):
                self._enqueue(f"recovery:{uuid4().hex}", "Housing page readable again", "The listings page is readable again. Scheduled monitoring still depends on the monitor being enabled.", channels)
                self.put("problem", None)
        return baseline, new

    def record_problem(self, kind: str, message: str, channels: tuple[str, ...]) -> bool:
        with self.db:
            old = self.get("problem")
            if old and old["kind"] == kind and old["message"] == message:
                return False
            self.put("problem", {"kind": kind, "message": message, "since": time.time()})
            self._enqueue(f"problem:{uuid4().hex}", "Housing monitor needs attention", message, channels)
        return True

    def pending(self, now: float | None = None) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM deliveries WHERE sent_at IS NULL AND next_attempt_at<=? ORDER BY id LIMIT 40",
            (time.time() if now is None else now,),
        ).fetchall()

    def delivered(self, delivery_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE deliveries SET sent_at=?, last_error=NULL, attempts=attempts+1 WHERE id=?", (time.time(), delivery_id))

    def delivery_failed(self, delivery_id: int, error: str) -> None:
        row = self.db.execute("SELECT attempts FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        attempts = row[0] + 1
        # Only delivery is retried. A failed Gmail delivery cannot trigger a
        # second successful Telegram notification or another website fetch.
        delay = min(3600, 300 * (2 ** min(attempts - 1, 4)))
        with self.db:
            self.db.execute("UPDATE deliveries SET attempts=?, next_attempt_at=?, last_error=? WHERE id=?", (attempts, time.time() + delay, error, delivery_id))

    def summary(self) -> dict:
        checks = self.db.execute("SELECT slot, outcome, error FROM checks ORDER BY started_at DESC LIMIT 1").fetchone()
        return {
            "baselined": self.get("baselined", False),
            "last_success": self.get("last_success"),
            "current_dortmund_count": self.get("current_dortmund_count"),
            "known_dortmund_offers": self.db.execute("SELECT COUNT(*) FROM offers").fetchone()[0],
            "pending_deliveries": self.db.execute("SELECT COUNT(*) FROM deliveries WHERE sent_at IS NULL").fetchone()[0],
            "problem": self.get("problem"),
            "last_check": dict(checks) if checks else None,
        }

