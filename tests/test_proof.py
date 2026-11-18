import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from studentwerk_monitor.proof import run_proof
from studentwerk_monitor.schedule import BERLIN


class ProofTests(unittest.TestCase):
    def test_one_time_proof_is_labelled_and_cannot_repeat(self):
        with tempfile.TemporaryDirectory() as folder:
            sender = Mock()
            when = datetime(2026, 11, 18, 22, 0, tzinfo=BERLIN)
            report = run_proof(Path(folder), "example", when, now=when, notifier=sender)
            self.assertEqual(report["status"], "finished")
            self.assertEqual(sender.send.call_count, 2)
            for call in sender.send.call_args_list:
                self.assertIn("Scheduler test only", call.args[1])
                self.assertIn("housing website was not accessed", call.args[2])
            self.assertTrue(run_proof(Path(folder), "example", when, now=when, notifier=sender)["already_executed"])
            self.assertEqual(sender.send.call_count, 2)

    def test_a_missed_calendar_trigger_cannot_send_a_later_day_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            sender = Mock()
            target = datetime(2026, 11, 18, 22, 0, tzinfo=BERLIN)
            later = datetime(2026, 11, 19, 22, 0, tzinfo=BERLIN)
            report = run_proof(Path(folder), "example", target, now=later, notifier=sender)
            self.assertEqual(report["status"], "missed-time-window")
            sender.send.assert_not_called()
