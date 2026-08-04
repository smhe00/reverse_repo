from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from repo_execution_core import OrderView  # noqa: E402
from repo_live_channel_validation import (  # noqa: E402
    plan_live_execution,
    sign_payload,
    validate_live_channel_evidence,
    verify_live_channel_certificate,
)


class LiveChannelValidationTests(unittest.TestCase):
    @staticmethod
    def order(
        *, remark: str = "repo_live_cert_20260805_0001", traded_volume: int = 10,
        order_volume: int = 10, status: int = 56,
    ) -> OrderView:
        return OrderView(
            order_id=109,
            symbol="204001.SH",
            order_type=24,
            status=status,
            order_volume=order_volume,
            traded_volume=traded_volume,
            traded_price=1.5,
            limit_price=1.5,
            status_msg="",
            strategy_name="repo_morning_v2",
            remark=remark,
        )

    @staticmethod
    def evidence(filled: int = 1000):
        journal = {
            "schema_version": 2,
            "strategy": "repo_morning_v2",
            "data": {
                "environment": "live",
                "live_channel_certification": True,
                "maximum_principal_yuan": 1000,
                "cash_usage_ratio": 1.0,
                "remark_prefix": "repo_live_cert_20260805_",
                "filled_principal_yuan": filled,
                "formal_verification": {
                    "transition_spec_sha256": "transition",
                    "execution_source_sha256": "source",
                },
            },
            "machine": {
                "state": "done_filled",
                "facts": {"unresolved_order": False},
            },
            "history": [
                {
                    "event": "intent_persisted",
                    "details": {"remark": "repo_live_cert_20260805_0001"},
                }
            ],
        }
        preflight = {
            "passed": True,
            "account_id_fingerprint": "account",
            "qmt_path_fingerprint": "path",
            "machine_fingerprint": "machine",
            "checks": {"connection_ok": True, "gc001_quote_ok": True},
        }
        return journal, preflight

    def test_windows_and_next_available_time(self):
        tz = timezone.utc
        trigger, unavailable = plan_live_execution(
            datetime(2026, 8, 5, 9, 29, 30, tzinfo=tz)
        )
        self.assertIsNone(unavailable)
        self.assertEqual(trigger.time().replace(tzinfo=None).isoformat(), "09:29:35")
        trigger, unavailable = plan_live_execution(
            datetime(2026, 8, 5, 11, 25, 1, tzinfo=tz)
        )
        self.assertEqual(trigger.time().replace(tzinfo=None).isoformat(), "12:59:30")
        self.assertEqual(trigger, unavailable)
        trigger, unavailable = plan_live_execution(
            datetime(2026, 8, 5, 15, 25, 1, tzinfo=tz)
        )
        self.assertEqual(trigger.date().isoformat(), "2026-08-06")
        self.assertEqual(trigger, unavailable)
        trigger, _ = plan_live_execution(
            datetime(2026, 8, 7, 15, 25, 1, tzinfo=tz)
        )
        self.assertEqual(trigger.date().isoformat(), "2026-08-10")

    def test_positive_terminal_fill_is_required_and_capped(self):
        journal, preflight = self.evidence()
        with mock.patch(
            "repo_live_channel_validation.machine_fingerprint",
            return_value="machine",
        ):
            evidence, checks = validate_live_channel_evidence(
                journal=journal,
                broker_orders=[self.order()],
                expected_account_fingerprint="account",
                expected_path_fingerprint="path",
                preflight=preflight,
                expected_source_hash="source",
                expected_transition_hash="transition",
            )
        self.assertEqual(evidence["filled_principal_yuan"], 1000)
        self.assertTrue(all(checks.values()))

        journal["data"]["filled_principal_yuan"] = 0
        zero_order = self.order(traded_volume=0, status=54)
        _, zero_checks = validate_live_channel_evidence(
            journal=journal,
            broker_orders=[zero_order],
            expected_account_fingerprint="account",
            expected_path_fingerprint="path",
            preflight=preflight,
            expected_source_hash="source",
            expected_transition_hash="transition",
        )
        self.assertFalse(zero_checks["positive_broker_fill"])
        self.assertFalse(zero_checks["journal_reports_positive_fill"])

    def test_unresolved_or_duplicate_broker_evidence_is_rejected(self):
        journal, preflight = self.evidence()
        active = self.order(traded_volume=0, status=50)
        _, checks = validate_live_channel_evidence(
            journal=journal,
            broker_orders=[active],
            expected_account_fingerprint="account",
            expected_path_fingerprint="path",
            preflight=preflight,
            expected_source_hash="source",
            expected_transition_hash="transition",
        )
        self.assertFalse(checks["all_orders_terminal"])
        duplicate = [self.order(), self.order()]
        _, checks = validate_live_channel_evidence(
            journal=journal,
            broker_orders=duplicate,
            expected_account_fingerprint="account",
            expected_path_fingerprint="path",
            preflight=preflight,
            expected_source_hash="source",
            expected_transition_hash="transition",
        )
        self.assertFalse(checks["all_intents_have_one_broker_order"])
        self.assertFalse(checks["positive_broker_fill"])

    def test_signed_certificate_rejects_evidence_and_environment_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "live_channel_20260805.journal.json"
            preflight = root / "preflight_20260805.json"
            journal.write_text("{}", encoding="utf-8")
            preflight.write_text("{}", encoding="utf-8")
            key_path = root / "key.json"
            key_path.write_text(
                json.dumps({"version": 1, "hmac_sha256_key_hex": "ab" * 32}),
                encoding="utf-8",
            )
            certificate = {
                "schema_version": 1,
                "certificate_type": "live_channel",
                "environment": "live",
                "passed": True,
                "certified_at": datetime.now(timezone.utc).isoformat(),
                "transition_spec_sha256": "transition",
                "execution_source_sha256": "source",
                "xtquant_runtime_sha256": "runtime",
                "account_id_fingerprint": "account",
                "qmt_path_fingerprint": "path",
                "machine_fingerprint": "machine",
                "filled_principal_yuan": 1000,
                "fixed_principal_limit_yuan": 1000,
                "broker_orders": [self.order().safe_payload()],
                "checks": {"evidence_ok": True},
                "evidence": {
                    "journal_name": journal.name,
                    "journal_sha256": __import__("hashlib").sha256(journal.read_bytes()).hexdigest(),
                    "preflight_name": preflight.name,
                    "preflight_sha256": __import__("hashlib").sha256(preflight.read_bytes()).hexdigest(),
                },
            }
            certificate["signature_hmac_sha256"] = sign_payload(
                certificate, bytes.fromhex("ab" * 32)
            )
            cert_path = root / "latest.json"
            cert_path.write_text(json.dumps(certificate), encoding="utf-8")
            binding = type(
                "Binding",
                (),
                {
                    "account_id_fingerprint": "account",
                    "qmt_path_fingerprint": "path",
                },
            )()
            with mock.patch(
                "repo_live_channel_validation.load_account_binding",
                return_value=binding,
            ), mock.patch(
                "repo_live_channel_validation.machine_fingerprint",
                return_value="machine",
            ):
                verify_live_channel_certificate(
                    certificate=certificate,
                    certificate_path=cert_path,
                    signing_key=key_path,
                    qmt_path=root,
                    account_binding=root / "binding.json",
                    expected_transition_hash="transition",
                    expected_source_hash="source",
                    expected_runtime_hash="runtime",
                )
                forged = dict(certificate)
                forged["filled_principal_yuan"] = 500
                forged["signature_hmac_sha256"] = sign_payload(
                    forged, bytes.fromhex("ab" * 32)
                )
                with self.assertRaisesRegex(RuntimeError, "broker-order fill"):
                    verify_live_channel_certificate(
                        certificate=forged,
                        certificate_path=cert_path,
                        signing_key=key_path,
                        qmt_path=root,
                        account_binding=root / "binding.json",
                        expected_transition_hash="transition",
                        expected_source_hash="source",
                        expected_runtime_hash="runtime",
                    )
                journal.write_text('{"tampered":true}', encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                    verify_live_channel_certificate(
                        certificate=certificate,
                        certificate_path=cert_path,
                        signing_key=key_path,
                        qmt_path=root,
                        account_binding=root / "binding.json",
                        expected_transition_hash="transition",
                        expected_source_hash="source",
                        expected_runtime_hash="runtime",
                    )


if __name__ == "__main__":
    unittest.main()
