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
    SmtpAlertConfig,
    _build_message,
    load_optional_smtp_failure_notifier,
    load_smtp_failure_notifier,
    notify_journal_certification,
    notify_journal_failure,
    notify_journal_success,
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
    def _config(self) -> SmtpAlertConfig:
        return SmtpAlertConfig(
            to_addresses=("operator@example.com",),
            from_address="sender@example.com",
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_security="starttls",
            smtp_username="sender@example.com",
            timeout_seconds=10.0,
            attempts=1,
        )

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

    def test_success_email_contains_trade_details_and_is_deduplicated(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.update_data(
                success=True,
                filled_principal_yuan=1000,
                current_order={
                    "symbol": "204001.SH",
                    "order_id": 12345,
                    "limit_price": 1.5,
                    "traded_price": 1.5,
                    "traded_volume": 10,
                },
            )
            notifier = _RecordingNotifier()
            first = notify_journal_success(
                notifier,
                journal,
                environment="simulation",
                state="done_filled",
            )
            second = notify_journal_success(
                notifier,
                journal,
                environment="simulation",
                state="done_filled",
            )
            self.assertEqual(first.status, "sent")
            self.assertEqual(second.status, "already_sent")
            self.assertEqual(len(notifier.alerts), 1)
            self.assertEqual(notifier.alerts[0].kind, "success")
            self.assertIn("成交本金=1000元", notifier.alerts[0].reason)
            self.assertEqual(
                journal.payload["data"]["success_alert"]["status"],
                "sent",
            )

    def test_certification_success_email_is_clear_and_structured(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.update_data(
                success=True,
                filled_principal_yuan=1000,
                current_order={
                    "symbol": "204001.SH",
                    "order_id": 12345,
                    "limit_price": 1.5,
                    "traded_price": 1.5,
                    "traded_volume": 10,
                },
            )
            notifier = _RecordingNotifier()
            delivery = notify_journal_certification(
                notifier,
                journal,
                environment="live",
                state="certificate_issued",
                passed=True,
            )
            self.assertEqual(delivery.status, "sent")
            alert = notifier.alerts[0]
            self.assertEqual(alert.kind, "success")
            self.assertTrue(alert.certification)
            message = _build_message(self._config(), alert)
            self.assertIn("[实盘认证成功]", message["Subject"])
            self.assertIn("实盘通道认证已通过", message.get_content())
            self.assertIn("成交本金（元）：1000", message.get_content())
            self.assertIn("rr on", message.get_content())

    def test_certification_failure_email_is_clear(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            notifier = _RecordingNotifier()
            delivery = notify_journal_certification(
                notifier,
                journal,
                environment="live",
                state="certification_failed",
                passed=False,
                reason="cannot reach a certifiable state",
            )
            self.assertEqual(delivery.status, "sent")
            alert = notifier.alerts[0]
            self.assertEqual(alert.kind, "failure")
            self.assertTrue(alert.certification)
            message = _build_message(self._config(), alert)
            self.assertIn("[实盘认证失败]", message["Subject"])
            self.assertIn("未通过", message.get_content())
            self.assertIn(
                "cannot reach a certifiable state",
                message.get_content(),
            )
            self.assertIn("rr cert", message.get_content())

    def test_configuration_test_email_is_clearly_marked(self):
        alert = FailureAlert(
            strategy="email_alert_configuration_test",
            trade_date="2026-08-05",
            environment="configuration_test",
            state="test",
            event="test",
            reason="no trading error occurred",
            unresolved_order=False,
            journal_path="not-applicable",
            occurred_at="2026-08-05T10:00:00+08:00",
        )
        message = _build_message(self._config(), alert)
        self.assertIn("[测试邮件]", message["Subject"])
        self.assertIn("无需任何处理", message.get_content())

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
