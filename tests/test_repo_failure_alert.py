from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from repo_execution_core import AtomicJournal  # noqa: E402
from repo_execution_state_machine import (  # noqa: E402
    initial_morning_snapshot,
    snapshot_to_payload,
)
from repo_failure_alert import (  # noqa: E402
    ALERT_PASSWORD_ENV,
    AlertConfigurationError,
    FailureAlert,
    load_optional_smtp_failure_notifier,
    load_smtp_failure_notifier,
    notify_journal_failure,
)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.alerts: list[FailureAlert] = []
        self.config = SimpleNamespace(attempts=1)

    def send(self, alert: FailureAlert) -> None:
        self.alerts.append(alert)


class _FailingNotifier:
    def __init__(self) -> None:
        self.config = SimpleNamespace(attempts=3)

    def send(self, alert: FailureAlert) -> None:
        del alert
        raise OSError("network unavailable")


class FailureAlertTests(unittest.TestCase):
    def test_optional_email_missing_or_invalid_never_raises(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            notifier, warning = load_optional_smtp_failure_notifier(missing)
            self.assertIsNone(notifier)
            self.assertIn("does not exist", warning or "")

            invalid = Path(directory) / "invalid.json"
            invalid.write_text("not-json", encoding="utf-8")
            notifier, warning = load_optional_smtp_failure_notifier(invalid)
            self.assertIsNone(notifier)
            self.assertIn("AlertConfigurationError", warning or "")

    def _journal(self, directory: str) -> AtomicJournal:
        journal = AtomicJournal(
            Path(directory) / "journal.json",
            strategy="test_strategy",
            trade_date=date(2026, 7, 31),
        )
        snapshot = initial_morning_snapshot()
        journal.load_or_initialize(
            machine_payload=snapshot_to_payload(snapshot),
            initial_data={"environment": "simulation"},
        )
        return journal

    def test_alert_is_sent_once_for_the_same_failure_key(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            notifier = _RecordingNotifier()
            first = notify_journal_failure(
                notifier,
                journal,
                environment="simulation",
                state="safe_halt",
                event="fault",
                reason="cannot recover",
                unresolved_order=True,
            )
            second = notify_journal_failure(
                notifier,
                journal,
                environment="simulation",
                state="safe_halt",
                event="fault",
                reason="cannot recover",
                unresolved_order=True,
            )
            self.assertEqual(first.status, "sent")
            self.assertEqual(second.status, "already_sent")
            self.assertEqual(len(notifier.alerts), 1)
            self.assertTrue(notifier.alerts[0].unresolved_order)
            self.assertEqual(
                journal.payload["data"]["failure_alert"]["status"],
                "sent",
            )

    def test_alert_failure_is_recorded_without_raising(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            delivery = notify_journal_failure(
                _FailingNotifier(),
                journal,
                environment="live",
                state="safe_halt",
                event="fault",
                reason="cannot recover",
                unresolved_order=False,
            )
            self.assertEqual(delivery.status, "failed")
            self.assertIn("network unavailable", delivery.error or "")
            self.assertEqual(
                journal.payload["data"]["failure_alert"]["status"],
                "failed",
            )

    def test_smtp_password_is_required_but_never_read_from_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mail.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "enabled": True,
                        "transport": "smtp",
                        "to": ["operator@example.com"],
                        "from": "sender@example.com",
                        "smtp_host": "smtp.example.com",
                        "smtp_port": 587,
                        "smtp_security": "starttls",
                        "smtp_username": "sender@example.com",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(AlertConfigurationError):
                load_smtp_failure_notifier(path, environ={})
            notifier = load_smtp_failure_notifier(
                path,
                environ={ALERT_PASSWORD_ENV: "secret-in-memory"},
            )
            self.assertEqual(
                notifier.config.to_addresses,
                ("operator@example.com",),
            )
            self.assertNotIn(
                "secret-in-memory",
                path.read_text(encoding="utf-8"),
            )

    def test_plaintext_smtp_transport_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mail.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "enabled": True,
                        "transport": "smtp",
                        "to": ["operator@example.com"],
                        "from": "sender@example.com",
                        "smtp_host": "smtp.example.com",
                        "smtp_port": 25,
                        "smtp_security": "plain",
                        "smtp_username": "",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(AlertConfigurationError):
                load_smtp_failure_notifier(path, environ={})


if __name__ == "__main__":
    unittest.main()
