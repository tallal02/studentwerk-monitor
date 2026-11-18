import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock

from studentwerk_monitor.cloud_test import SECRET_FIELDS, run_test
from studentwerk_monitor.notifications import NotificationError


class CloudNotificationTests(unittest.TestCase):
    def environment(self):
        return {**{name: "example" for name in SECRET_FIELDS.values()}, "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "example/example", "GITHUB_RUN_ID": "123"}

    def test_cloud_proof_refuses_to_impersonate_a_cloud_run_locally(self):
        sender = Mock()
        with self.assertRaises(ValueError):
            run_test({}, notifier=sender)
        sender.send.assert_not_called()

    def test_cloud_proof_is_labelled_and_has_a_verifiable_run_link(self):
        sender = Mock()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run_test(self.environment(), notifier=sender), {"telegram": True, "gmail": True})
        self.assertEqual(sender.send.call_count, 2)
        for call in sender.send.call_args_list:
            self.assertIn("Cloud test only", call.args[1])
            self.assertIn("https://github.com/example/example/actions/runs/123", call.args[2])
            self.assertIn("housing website was not accessed", call.args[2])

    def test_cloud_proof_reports_one_channel_failure_and_still_tests_the_other(self):
        sender = Mock()
        sender.send.side_effect = [NotificationError("Unavailable"), None]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run_test(self.environment(), notifier=sender), {"telegram": False, "gmail": True})

