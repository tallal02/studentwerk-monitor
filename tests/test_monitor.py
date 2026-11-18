import json
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from studentwerk_monitor.agent import build_plist
from studentwerk_monitor.app import CheckDeferred, check_once, main, process_tick, read_offers, require_tested, setup
from studentwerk_monitor.browser import OFFERS_URL, AccessDenied, BraveBrowser, BrowserError, Offer, VerificationRequired, parse_snapshot
from studentwerk_monitor.config import ConfigurationError, config_fingerprint, load_json, notification_config, save_json
from studentwerk_monitor.notifications import NotificationError, deliver_pending, telegram_request
from studentwerk_monitor.schedule import BERLIN, due_slot, next_slot
from studentwerk_monitor.state import State


def german(value):
    return datetime.fromisoformat(value).replace(tzinfo=BERLIN)


def offer(identifier="6824", city="Dortmund"):
    return Offer(identifier, f"https://www.stwdo.de/freie-zimmer/{identifier}", f"{city}, Vogelpothsweg, 118", "2 Zimmer in 2er-WG\n30qm\n431 € pauschal")


def snapshot(offers=None):
    offers = [offer(), offer("6750", "Iserlohn")] if offers is None else offers
    return {
        "url": OFFERS_URL,
        "readyState": "complete",
        "verification": False,
        "text": "Freie Zimmer in unseren Wohnanlagen\nFreie Zimmer",
        "offers": [{"url": item.url, "text": f"{item.title}\n\n{item.details}\nMehr Informationen"} for item in offers],
    }


class ScheduleTests(unittest.TestCase):
    def test_weekly_boundaries(self):
        cases = [
            ("2026-11-16T08:59:59", None),
            ("2026-11-16T09:00:00", "2026-11-16T09:00:00+01:00"),
            ("2026-11-16T09:59:59", "2026-11-16T09:00:00+01:00"),
            ("2026-11-17T00:05:00", "2026-11-17T00:00:00+01:00"),
            ("2026-11-20T23:29:59", "2026-11-20T23:00:00+01:00"),
            ("2026-11-20T23:30:03", "2026-11-20T23:30:00+01:00"),
            ("2026-11-20T23:31:00", None),
            ("2026-11-21T10:00:00", None),
            ("2026-11-22T23:59:59", None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                actual = due_slot(german(value))
                self.assertEqual(actual.isoformat() if actual else None, expected)

    def test_timezone_is_berlin_even_when_computer_timestamp_is_utc(self):
        winter = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 6, 7, 0, tzinfo=timezone.utc)
        self.assertEqual(due_slot(winter).hour, 9)
        self.assertEqual(due_slot(summer).hour, 9)

    def test_next_slot_and_daylight_saving(self):
        self.assertEqual(next_slot(german("2026-11-20T23:05:00")), german("2026-11-20T23:30:00"))
        self.assertEqual(next_slot(german("2026-11-20T23:30:00")), german("2026-11-23T09:00:00"))
        spring = next_slot(german("2026-03-27T23:30:00"))
        self.assertEqual(spring.isoformat(), "2026-03-30T09:00:00+02:00")
        autumn = next_slot(german("2026-10-23T23:30:00"))
        self.assertEqual(autumn.isoformat(), "2026-10-26T09:00:00+01:00")

    def test_naive_times_are_rejected(self):
        for function in (due_slot, next_slot):
            with self.assertRaises(ValueError):
                function(datetime(2026, 11, 16, 9))


class ParserTests(unittest.TestCase):
    def test_actual_card_format_and_city_filter(self):
        values = parse_snapshot(snapshot())
        self.assertEqual([item.id for item in values], ["6824", "6750"])
        self.assertEqual([item.id for item in values if item.is_dortmund], ["6824"])
        self.assertNotIn("Mehr Informationen", values[0].details)
        self.assertFalse(offer(city="Not Dortmund").is_dortmund)
        self.assertFalse(offer(city="Dortmunder Straße").is_dortmund)

    def test_verified_empty_page_is_allowed(self):
        self.assertEqual(parse_snapshot(snapshot([])), [])

    def test_verification_error_and_wrong_pages_are_not_empty_results(self):
        cases = [
            {"verification": True},
            {"text": "Server temporarily unavailable"},
            {"readyState": "loading"},
            {"responseStatus": 403, "text": "Forbidden"},
            {"responseStatus": 429, "text": "Too many requests"},
            {"responseStatus": 503, "text": "Unavailable"},
            {"url": "https://example.com/wohnen/aktuelle-wohnangebote"},
            {"offers": [{"url": "https://example.com/freie-zimmer/6824", "text": "Dortmund, room"}]},
            {"offers": [], "text": "Freie Zimmer in unseren Wohnanlagen\nDortmund\nMehr Informationen"},
            {"offers": [{"url": "https://www.stwdo.de/freie-zimmer/123", "text": ""}]},
        ]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(BrowserError):
                parse_snapshot({**snapshot(), **change})


class BrowserPollingTests(unittest.TestCase):
    @patch("studentwerk_monitor.browser.time.sleep")
    @patch("studentwerk_monitor.browser.uuid4")
    def test_slow_refresh_does_not_reuse_stale_dom(self, identifier, sleep):
        identifier.return_value.hex = "test-marker"
        browser = BraveBrowser(timeout=1)
        browser.execute = Mock(return_value="refresh-requested")
        browser.snapshot = Mock(side_effect=[
            {**snapshot([offer("100")]), "refreshMarker": "test-marker"},
            snapshot([offer("200")]),
        ])
        self.assertEqual([item.id for item in browser.fetch()], ["200"])
        self.assertEqual(browser.snapshot.call_count, 2)

    @patch("studentwerk_monitor.browser.time.sleep")
    def test_verification_stops_without_clicks_or_form_submission(self, sleep):
        browser = BraveBrowser(timeout=1)
        browser.execute = Mock(return_value="navigation-requested")
        gate = {**snapshot(), "verification": True}
        browser.snapshot = Mock(return_value=gate)
        with self.assertRaises(VerificationRequired):
            browser.fetch()
        browser.execute.assert_called_once()
        browser.snapshot.assert_called_once()

    @patch("studentwerk_monitor.browser.time.sleep")
    def test_forbidden_page_stops_the_check_immediately(self, sleep):
        browser = BraveBrowser(timeout=1)
        browser.execute = Mock(return_value="refresh-requested")
        browser.snapshot = Mock(return_value={**snapshot(), "responseStatus": 403})
        with self.assertRaises(AccessDenied):
            browser.fetch()
        browser.execute.assert_called_once()
        browser.snapshot.assert_called_once()


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "data"
        self.state = State(self.home)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_baseline_then_only_new_dortmund_offer(self):
        baseline, new = self.state.record_snapshot([offer(), offer("6750", "Iserlohn")], ("telegram", "gmail"))
        self.assertTrue(baseline)
        self.assertEqual(new, [])
        self.assertEqual(self.state.pending(), [])
        baseline, new = self.state.record_snapshot([offer("6750", "Iserlohn"), offer(), offer("7000")], ("telegram", "gmail"))
        self.assertFalse(baseline)
        self.assertEqual([item.id for item in new], ["7000"])
        self.assertEqual(len(self.state.pending()), 2)
        self.assertEqual(self.state.summary()["known_dortmund_offers"], 2)
        for row in self.state.pending():
            self.assertIn("/7000", json.loads(row["payload"])["body"])

    def test_duplicates_and_reappearing_known_offer_survive_restart(self):
        self.state.record_snapshot([], ("telegram", "gmail"))
        self.state.record_snapshot([offer()], ("telegram", "gmail"))
        self.state.close()
        self.state = State(self.home)
        self.state.record_snapshot([], ("telegram", "gmail"))
        self.state.record_snapshot([offer(), offer()], ("telegram", "gmail"))
        self.assertEqual(len(self.state.pending()), 2)

    def test_delivery_failure_only_retries_failed_channel(self):
        self.state.record_snapshot([], ("telegram", "gmail"))
        self.state.record_snapshot([offer()], ("telegram", "gmail"))
        notifier = Mock()
        notifier.send.side_effect = [None, NotificationError("Gmail unavailable")]
        self.assertEqual(deliver_pending(self.state, notifier), (1, 1))
        self.assertEqual(self.state.pending(), [])
        self.assertEqual(len(self.state.pending(now=10**12)), 1)
        with self.state.db:
            self.state.db.execute("UPDATE deliveries SET next_attempt_at=0 WHERE sent_at IS NULL")
        notifier.send.reset_mock(side_effect=True)
        self.assertEqual(deliver_pending(self.state, notifier), (1, 0))
        self.assertEqual(notifier.send.call_args.args[0], "gmail")
        self.assertEqual(self.state.summary()["pending_deliveries"], 0)

    @patch("studentwerk_monitor.app.local_notification")
    def test_verification_failure_keeps_history_and_emits_one_incident(self, notification):
        self.state.record_snapshot([offer()], ("telegram", "gmail"))
        browser = Mock()
        browser.fetch.side_effect = VerificationRequired("Please verify")
        for _ in range(2):
            self.assertFalse(check_once(self.state, browser, ("telegram", "gmail"))["ok"])
        self.assertEqual(self.state.summary()["known_dortmund_offers"], 1)
        self.assertEqual(self.state.summary()["current_dortmund_count"], 1)
        self.assertEqual(len(self.state.pending()), 2)
        notification.assert_called_once()
        self.state.record_snapshot([offer()], ("telegram", "gmail"))
        self.assertIsNone(self.state.get("problem"))
        self.assertEqual(len(self.state.pending()), 4)

    def test_one_fetch_per_hour_and_special_friday_slot(self):
        browser = Mock()
        browser.fetch.return_value = [offer()]
        notifier = Mock()
        for value in ("2026-11-20T23:00:00", "2026-11-20T23:05:00", "2026-11-20T23:25:00", "2026-11-20T23:30:02", "2026-11-20T23:31:00"):
            process_tick(self.state, {}, browser, german(value), notifier)
        self.assertEqual(browser.fetch.call_count, 2)

    def test_weekend_and_monday_before_nine_do_not_touch_browser(self):
        browser, notifier = Mock(), Mock()
        for value in ("2026-11-21T12:00:00", "2026-11-22T23:59:00", "2026-11-23T08:59:59"):
            result = process_tick(self.state, {}, browser, german(value), notifier)
            self.assertFalse(result["active"])
        browser.fetch.assert_not_called()
        notifier.send.assert_not_called()

    def test_waking_up_does_one_current_check_without_replaying_missed_hours(self):
        browser, notifier = Mock(), Mock()
        browser.fetch.return_value = [offer()]
        result = process_tick(self.state, {}, browser, german("2026-11-18T14:43:00"), notifier)
        self.assertTrue(result["checked"])
        process_tick(self.state, {}, browser, german("2026-11-18T14:48:00"), notifier)
        browser.fetch.assert_called_once()

    def test_private_storage_permissions(self):
        save_json(self.home / "credentials.json", {"example": "not-a-real-secret"})
        self.assertEqual((self.home.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((self.home / "credentials.json").stat().st_mode & 0o777), 0o600)
        self.assertEqual(load_json(self.home / "credentials.json"), {"example": "not-a-real-secret"})

    @patch("studentwerk_monitor.app.local_notification")
    def test_access_refusal_persistently_pauses_later_checks(self, notification):
        browser = Mock()
        browser.fetch.side_effect = AccessDenied("HTTP 403")
        self.assertFalse(check_once(self.state, browser, (), now=german("2026-11-18T14:00:00"))["ok"])
        self.assertTrue(self.state.get("access_paused"))
        self.assertTrue(check_once(self.state, browser, ())["paused"])
        self.state.close()
        self.state = State(self.home)
        self.assertTrue(process_tick(self.state, {}, browser, german("2026-11-18T15:00:00"), Mock())["paused"])
        browser.fetch.assert_called_once()
        browser.fetch.side_effect = None
        browser.fetch.return_value = [offer()]
        self.assertTrue(check_once(self.state, browser, (), allow_paused=True, now=german("2026-11-18T16:00:00"))["ok"])
        self.assertFalse(self.state.get("access_paused"))

    def test_notification_setup_never_accesses_housing_or_enables_agent(self):
        with self.state.db:
            self.state.put("access_paused", True)
        with (
            patch("studentwerk_monitor.app.agent_running", return_value=False),
            patch("studentwerk_monitor.app.BraveBrowser") as browser,
            patch("studentwerk_monitor.app.install_agent") as install,
            patch("studentwerk_monitor.app._secret", side_effect=["123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "abcdefghijklmnop"]),
            patch("studentwerk_monitor.app._input", side_effect=["example@gmail.com", "example@gmail.com"]),
            patch("builtins.input", return_value=""),
            patch("studentwerk_monitor.app.telegram_request", side_effect=[{"username": "example_bot"}, [{"message": {"chat": {"id": 123, "type": "private"}, "from": {"id": 123}}}]]),
            patch("studentwerk_monitor.app.test_alerts"),
            redirect_stdout(io.StringIO()),
        ):
            setup(self.home, self.state)
        browser.assert_not_called()
        install.assert_not_called()
        self.assertTrue(self.state.get("access_paused"))

    def test_activation_requires_baseline_and_both_verified_channels(self):
        with self.assertRaises(ConfigurationError):
            require_tested(self.home, self.state)
        save_json(self.home / "config.json", {"telegram_chat_id": "123", "gmail_address": "example@gmail.com", "gmail_recipient": "example@gmail.com"})
        save_json(self.home / "credentials.json", {"telegram_token": "example", "gmail_app_password": "example"})
        config = notification_config(self.home)
        with self.state.db:
            self.state.put("notification_tests", {"fingerprint": config_fingerprint(config), "results": {"telegram": True, "gmail": False}})
        with self.assertRaises(ConfigurationError):
            require_tested(self.home, self.state)
        self.state.record_snapshot([], ())
        with self.state.db:
            self.state.put("notification_tests", {"fingerprint": config_fingerprint(config), "results": {"telegram": True, "gmail": True}})
        require_tested(self.home, self.state)
        save_json(self.home / "credentials.json", {"telegram_token": "changed", "gmail_app_password": "example"})
        with self.assertRaises(ConfigurationError):
            require_tested(self.home, self.state)


class RequestGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "data"
        self.state = State(self.home)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_manual_check_and_schedule_share_cooldown_across_restart(self):
        browser, notifier = Mock(), Mock()
        browser.fetch.return_value = [offer()]
        read_offers(self.state, browser, now=german("2026-11-18T14:55:00"))
        self.state.close()
        self.state = State(self.home)
        result = process_tick(self.state, {}, browser, german("2026-11-18T15:00:00"), notifier)
        self.assertTrue(result["deferred"])
        self.assertFalse(result["checked"])
        process_tick(self.state, {}, browser, german("2026-11-18T15:05:00"), notifier)
        browser.fetch.assert_called_once()
        process_tick(self.state, {}, browser, german("2026-11-18T16:00:00"), notifier)
        self.assertEqual(browser.fetch.call_count, 2)
        with self.assertRaises(CheckDeferred):
            read_offers(self.state, browser, now=german("2026-11-18T16:05:00"))
        self.assertEqual(browser.fetch.call_count, 2)

    @patch("studentwerk_monitor.app.local_notification")
    def test_failure_still_consumes_attempt_and_stops_immediate_retry(self, notification):
        browser = Mock()
        browser.fetch.side_effect = BrowserError("Unavailable")
        result = check_once(self.state, browser, (), now=german("2026-11-18T14:00:00"))
        self.assertFalse(result["ok"])
        result = check_once(self.state, browser, (), now=german("2026-11-18T14:01:00"))
        self.assertTrue(result["deferred"])
        browser.fetch.assert_called_once()

    def test_recovery_command_requires_manual_recovery_confirmation(self):
        with self.state.db:
            self.state.put("access_paused", True)
        with patch("studentwerk_monitor.app.BraveBrowser") as browser, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--home", str(self.home), "resume-access"]), 1)
        browser.assert_not_called()
        self.assertTrue(self.state.get("access_paused"))

    def test_paused_website_does_not_prevent_queued_error_delivery(self):
        with self.state.db:
            self.state.put("access_paused", True)
        self.state.record_problem("blocked", "HTTP 403", ("telegram", "gmail"))
        browser, notifier = Mock(), Mock()
        result = process_tick(self.state, {}, browser, german("2026-11-18T15:00:00"), notifier)
        self.assertTrue(result["paused"])
        self.assertEqual(result["sent"], 2)
        browser.fetch.assert_not_called()


class DeliveryAndAgentTests(unittest.TestCase):
    @patch("studentwerk_monitor.notifications.urlopen")
    def test_request_errors_do_not_reveal_telegram_token(self, request):
        from urllib.error import URLError
        token = "123456:PRIVATE_TOKEN_VALUE"
        request.side_effect = URLError(f"https://api.telegram.org/bot{token}/sendMessage failed")
        with self.assertRaises(NotificationError) as caught:
            telegram_request(token, "sendMessage", {})
        self.assertNotIn(token, str(caught.exception))

    def test_agent_has_no_credentials_in_arguments_and_retries_are_local(self):
        plist = build_plist(Path("/tmp/example home"), "/usr/bin/python3")
        self.assertEqual(plist["StartCalendarInterval"], [{"Minute": value} for value in range(0, 60, 5)])
        self.assertEqual(plist["ProgramArguments"][-1], "tick")
        self.assertNotIn("credentials", " ".join(plist["ProgramArguments"]))


if __name__ == "__main__":
    unittest.main()

