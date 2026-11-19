import importlib.util
import io
import os
import tempfile
import threading
import unittest
from datetime import datetime
from contextlib import redirect_stdout
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from studentwerk_monitor.browser import AccessDenied, BrowserError, Offer, VerificationRequired
from studentwerk_monitor.cloud import run_worker
from studentwerk_monitor.cloud_schedule import cloud_due_slot, next_cloud_slot
from studentwerk_monitor.cloud_test import SECRET_FIELDS
from studentwerk_monitor.config import ConfigurationError
from studentwerk_monitor.reader import CloudReader, parse_html
from studentwerk_monitor.schedule import BERLIN
from studentwerk_monitor.sealed_state import restore_state, seal_state
from studentwerk_monitor.state import State


def when(value):
    return datetime.fromisoformat(value).replace(tzinfo=BERLIN)


HTML = '''<html><body><nav><a href="/freie-zimmer/999">Ignored navigation</a></nav>
<main><h1>Freie Zimmer in unseren Wohnanlagen</h1>
<script>Ignore script contents</script><section>
<a href="/freie-zimmer/7001"><h3>Dortmund, <span>Example Street 1</span></h3>
<p>2er-WG</p><p>20 m²</p><p>400 € pauschal</p><span>Mehr Informationen</span></a>
<a href="https://www.stwdo.de/freie-zimmer/7002"><h3>Iserlohn, Example Street 2</h3><p>Apartment</p></a>
</section></main></body></html>'''


class CloudReaderTests(unittest.TestCase):
    def test_reads_main_cards_and_preserves_inline_title(self):
        offers = parse_html(HTML)
        self.assertEqual([offer.id for offer in offers], ["7001", "7002"])
        self.assertEqual(offers[0].title, "Dortmund, Example Street 1")
        self.assertEqual([offer.id for offer in offers if offer.is_dortmund], ["7001"])
        self.assertIn("400 €", offers[0].details)

    def test_verification_is_not_an_empty_list(self):
        with self.assertRaises(VerificationRequired):
            parse_html('<main><form id="housing-offers-access-form">Please verify</form></main>')

    def test_unsupported_html_is_rejected(self):
        with self.assertRaises(BrowserError):
            parse_html("<html><body>Something went wrong</body></html>")

    def test_refusals_and_timeouts_make_only_one_attempt(self):
        errors = [HTTPError("https://example.invalid", 403, "Forbidden", {}, None), HTTPError("https://example.invalid", 429, "Too many requests", {}, None), URLError("timeout")]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                opener = Mock()
                opener.open.side_effect = error
                with self.assertRaises(BrowserError):
                    CloudReader(opener).fetch()
                opener.open.assert_called_once()

    def test_successful_http_response_is_parsed_with_one_fetch(self):
        headers = Message()
        headers["Content-Type"] = "text/html; charset=utf-8"
        response = Mock()
        response.headers = headers
        response.read.return_value = HTML.encode()
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        self.assertEqual(len(CloudReader(opener).fetch()), 2)
        opener.open.assert_called_once()

    def test_redirect_does_not_send_a_second_request(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/second")
                self.end_headers()
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/first"
            with patch("studentwerk_monitor.reader.OFFERS_URL", url), self.assertRaises(BrowserError):
                CloudReader().fetch()
            self.assertEqual(requests, ["/first"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class CloudScheduleTests(unittest.TestCase):
    def test_week_boundaries_and_final_check(self):
        cases = [("2026-11-16T08:59:00", None), ("2026-11-16T09:00:00", 9), ("2026-11-16T10:59:00", 9), ("2026-11-16T11:00:00", 11), ("2026-11-17T00:30:00", None), ("2026-11-17T01:00:00", 1), ("2026-11-20T22:30:00", 21), ("2026-11-20T23:30:00", 23), ("2026-11-20T23:31:00", None), ("2026-11-21T09:00:00", None)]
        for value, expected in cases:
            with self.subTest(value=value):
                slot = cloud_due_slot(when(value))
                self.assertEqual(slot.hour if slot else None, expected)
        self.assertEqual(cloud_due_slot(when("2026-11-20T23:30:00")).minute, 30)

    def test_next_cloud_slot_handles_weekends_and_dst(self):
        self.assertEqual(next_cloud_slot(when("2026-11-20T21:00:00")), when("2026-11-20T23:30:00"))
        self.assertEqual(next_cloud_slot(when("2026-03-27T23:30:00")).isoformat(), "2026-03-30T09:00:00+02:00")


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "Cloud encryption dependency is installed by the monitor workflow")
class SealedStateTests(unittest.TestCase):
    def test_unexpected_worker_error_preserves_pause_without_printing_error_details(self):
        from cryptography.fernet import Fernet
        from studentwerk_monitor.cloud import main
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = Fernet.generate_key().decode()
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "CLOUD_STATE_KEY": key, "CLOUD_MODE": "status"}, clear=True),
                patch("studentwerk_monitor.cloud.Path", side_effect=lambda name: root / name),
                patch("studentwerk_monitor.cloud.run_worker", side_effect=RuntimeError("private diagnostic value")),
                redirect_stdout(output),
            ):
                self.assertEqual(main(), 1)
            self.assertNotIn("private diagnostic value", output.getvalue())
            restore_state(root / ".cloud-state.enc", root / "restored", key)
            restored = State(root / "restored")
            self.assertTrue(restored.get("access_paused"))
            restored.close()

    def test_roundtrip_keeps_history_and_pause_private(self):
        from cryptography.fernet import Fernet
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = Fernet.generate_key().decode()
            state = State(root / "original")
            with state.db:
                state.put("access_paused", True)
                state.put("private_marker", "private history value")
            state.close()
            encrypted = root / "state.enc"
            seal_state(root / "original", encrypted, key)
            self.assertNotIn(b"private history value", encrypted.read_bytes())
            self.assertTrue(restore_state(encrypted, root / "restored", key))
            restored = State(root / "restored")
            self.assertTrue(restored.get("access_paused"))
            self.assertEqual(restored.get("private_marker"), "private history value")
            restored.close()

    def test_invalid_key_and_modified_cache_fail_closed(self):
        from cryptography.fernet import Fernet
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = Fernet.generate_key().decode()
            state = State(root / "original")
            state.close()
            encrypted = root / "state.enc"
            seal_state(root / "original", encrypted, key)
            with self.assertRaises(ConfigurationError):
                restore_state(encrypted, root / "wrong-key", Fernet.generate_key().decode())
            data = bytearray(encrypted.read_bytes())
            data[len(data) // 2] ^= 1
            encrypted.write_bytes(data)
            with self.assertRaises(ConfigurationError):
                restore_state(encrypted, root / "modified", key)


class CloudWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / "state")
        self.reader = Mock()
        self.reader.fetch.return_value = [Offer("7001", "https://www.stwdo.de/freie-zimmer/7001", "Dortmund, Example Street", "400 €")]
        self.notifier = Mock()
        self.environment = {name: "example" for name in SECRET_FIELDS.values()}

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def run_worker(self, mode="tick", value="2026-11-18T09:00:00"):
        return run_worker(self.state, mode, self.environment, now=when(value), reader=self.reader, notifier=self.notifier)

    def activate_fixture(self):
        with self.state.db:
            self.state.put("cloud_initialized", True)
            self.state.put("access_paused", False)
        self.environment["LIVE_HOUSING_ENABLED"] = "true"

    def test_fresh_cache_and_disabled_mode_never_fetch(self):
        self.assertTrue(self.run_worker()["disabled"])
        self.environment["LIVE_HOUSING_ENABLED"] = "true"
        self.assertTrue(self.run_worker()["access_paused"])
        self.reader.fetch.assert_not_called()
        self.notifier.send.assert_not_called()

    def test_status_never_fetches_even_with_live_flag(self):
        self.activate_fixture()
        self.run_worker("status")
        self.reader.fetch.assert_not_called()
        self.notifier.send.assert_not_called()

    def test_recovery_requires_explicit_confirmation(self):
        with self.assertRaises(ConfigurationError):
            self.run_worker("recover-access")
        self.reader.fetch.assert_not_called()
        self.environment["MANUAL_ACCESS_CONFIRMED"] = "true"
        result = self.run_worker("recover-access")
        self.assertTrue(result["ok"])
        self.assertFalse(result["access_paused"])
        self.reader.fetch.assert_called_once()

    @patch("studentwerk_monitor.app.local_notification")
    def test_verification_pauses_all_future_scheduled_fetches(self, notification):
        self.environment["MANUAL_ACCESS_CONFIRMED"] = "true"
        self.reader.fetch.side_effect = VerificationRequired("Manual verification required")
        self.assertFalse(self.run_worker("recover-access")["ok"])
        self.environment["LIVE_HOUSING_ENABLED"] = "true"
        self.assertTrue(self.run_worker(value="2026-11-18T11:00:00")["access_paused"])
        self.reader.fetch.assert_called_once()

    def test_two_hour_spacing_and_slot_deduplication(self):
        self.activate_fixture()
        self.assertTrue(self.run_worker()["ok"])
        self.assertTrue(self.run_worker(value="2026-11-18T10:00:00")["already_checked"])
        self.assertTrue(self.run_worker(value="2026-11-18T11:00:00")["ok"])
        self.assertEqual(self.reader.fetch.call_count, 2)

    def test_late_job_cannot_cause_a_request_burst(self):
        self.activate_fixture()
        self.run_worker(value="2026-11-18T10:55:00")
        self.assertTrue(self.run_worker(value="2026-11-18T11:05:00")["deferred"])
        self.reader.fetch.assert_called_once()

    def test_weekend_never_fetches(self):
        self.activate_fixture()
        self.assertTrue(self.run_worker(value="2026-11-21T09:00:00")["outside_window"])
        self.reader.fetch.assert_not_called()

    def test_staged_validation_waits_until_approved_time_and_then_activates(self):
        self.environment["LIVE_HOUSING_ENABLED"] = "true"
        self.environment["VALIDATION_NOT_BEFORE"] = when("2026-11-19T01:00:00").isoformat()
        self.assertTrue(self.run_worker(value="2026-11-18T23:00:00")["waiting_for_validation_time"])
        self.reader.fetch.assert_not_called()
        result = self.run_worker(value="2026-11-19T01:00:00")
        self.assertTrue(result["ok"])
        self.assertFalse(result["access_paused"])
        self.assertEqual(result["mode"], "scheduled-validation")
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertTrue(self.run_worker(value="2026-11-19T03:00:00")["ok"])
        self.assertEqual(self.reader.fetch.call_count, 2)

    @patch("studentwerk_monitor.app.local_notification")
    def test_failed_staged_validation_cannot_repeat(self, notification):
        self.environment["LIVE_HOUSING_ENABLED"] = "true"
        self.environment["VALIDATION_NOT_BEFORE"] = when("2026-11-19T01:00:00").isoformat()
        self.reader.fetch.side_effect = AccessDenied("Refused")
        self.assertFalse(self.run_worker(value="2026-11-19T01:00:00")["ok"])
        self.assertTrue(self.run_worker(value="2026-11-19T03:00:00")["access_paused"])
        self.reader.fetch.assert_called_once()

    def test_expired_staged_validation_makes_no_request(self):
        self.environment["LIVE_HOUSING_ENABLED"] = "true"
        self.environment["VALIDATION_NOT_BEFORE"] = when("2026-11-19T01:00:00").isoformat()
        with self.assertRaises(ConfigurationError):
            self.run_worker(value="2026-11-19T03:01:00")
        self.reader.fetch.assert_not_called()


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "Browser fixture tests run in the isolated cloud job")
class RealBrowserFixtureTests(unittest.TestCase):
    def test_normal_consent_auto_submit_is_not_replayed_and_session_is_retained(self):
        from studentwerk_monitor.cloud_browser import CloudBrowserReader
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def send_html(self, html):
                data = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                if self.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                requests.append(("GET", self.path))
                if self.path == "/rooms" or "access=ok" in self.headers.get("Cookie", ""):
                    self.send_html(HTML)
                else:
                    self.send_html('''<main><form id="housing-offers-access-form" method="POST">
                    <input id="consent" type="checkbox" required style="pointer-events:none" name="_mosparo_checkboxField_test" onchange="document.getElementById('status').innerText='Ihre Daten enthalten kein Spam';this.form.requestSubmit()">
                    <label for="consent">Accept the test consent</label>
                    <p id="status"></p></form></main>''')
            def do_POST(self):
                requests.append(("POST", self.path))
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(303)
                self.send_header("Location", "/rooms")
                self.send_header("Set-Cookie", "access=ok; Path=/")
                self.end_headers()
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            options = {"url": f"http://127.0.0.1:{server.server_port}/gate", "delay_seconds": 0, "executable_path": os.environ.get("STUDENTWERK_TEST_BROWSER")}
            reader = CloudBrowserReader(**options)
            self.assertEqual(len(reader.fetch()), 2)
            self.assertEqual(requests, [("GET", "/gate"), ("POST", "/gate"), ("GET", "/rooms")])
            second = CloudBrowserReader(storage_state=reader.storage_state, **options)
            self.assertEqual(len(second.fetch()), 2)
            self.assertEqual(requests[-1], ("GET", "/gate"))
            self.assertEqual(sum(method == "POST" for method, _ in requests), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
