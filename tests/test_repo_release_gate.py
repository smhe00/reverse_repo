from __future__ import annotations

import hashlib
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from repo_simulation_validation import (  # noqa: E402
    _load_signing_key,
    _sign_payload,
)
from repo_execution_core import (  # noqa: E402
    reverse_repo_schedule_config_sha256,
)
from verify_repo_release_gate import (  # noqa: E402
    _verify_certificate_timestamp,
    _verify_evidence,
    _verify_signature,
    _verify_schedule_configuration,
)


class ReleaseGateAuthenticationTests(unittest.TestCase):
    def test_ratio_change_does_not_invalidate_capability_certificate(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "runtime.json"
            config_path.write_text(
                json.dumps(
                    {
                        "first_execution_time": "09:30:42",
                        "second_execution_time": "15:10:00",
                        "first_cash_usage_ratio": 0.90,
                    }
                ),
                encoding="utf-8",
            )
            certificate = {
                "schedule_config_sha256": (
                    reverse_repo_schedule_config_sha256(config_path)
                )
            }
            _verify_schedule_configuration(certificate, config_path)

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["first_cash_usage_ratio"] = 0.80
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            _verify_schedule_configuration(certificate, config_path)

    def test_time_change_invalidates_capability_certificate(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "runtime.json"
            payload = {
                "first_execution_time": "09:30:42",
                "second_execution_time": "15:10:00",
                "first_cash_usage_ratio": 0.90,
                "second_cash_usage_ratio": 1.0,
            }
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            certificate = {
                "schedule_config_sha256": (
                    reverse_repo_schedule_config_sha256(config_path)
                )
            }
            payload["first_execution_time"] = "09:31:00"
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError,
                "execution schedule",
            ):
                _verify_schedule_configuration(certificate, config_path)

    def test_invalid_ratio_is_rejected_even_when_schedule_matches(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "runtime.json"
            payload = {
                "first_execution_time": "09:30:42",
                "second_execution_time": "15:10:00",
                "first_cash_usage_ratio": 0.90,
                "second_cash_usage_ratio": 1.0,
            }
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            certificate = {
                "schedule_config_sha256": (
                    reverse_repo_schedule_config_sha256(config_path)
                )
            }
            payload["first_cash_usage_ratio"] = -0.01
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "from 0 through 1"):
                _verify_schedule_configuration(certificate, config_path)

    def test_matching_certificate_does_not_expire_by_calendar_age(self):
        now = datetime(2026, 8, 3, 15, 32, tzinfo=timezone.utc)
        _verify_certificate_timestamp(
            {
                "certified_at": (
                    now - timedelta(days=3650)
                ).isoformat(),
            },
            now=now,
        )

    def test_certificate_from_the_future_is_rejected(self):
        now = datetime(2026, 8, 3, 15, 32, tzinfo=timezone.utc)
        with self.assertRaisesRegex(RuntimeError, "future"):
            _verify_certificate_timestamp(
                {
                    "certified_at": (
                        now + timedelta(minutes=6)
                    ).isoformat(),
                },
                now=now,
            )

    def test_certificate_signature_and_evidence_reject_tampering(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            key_path = root / "key.json"
            key_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "hmac_sha256_key_hex": "ab" * 32,
                    }
                ),
                encoding="utf-8",
            )
            morning = root / "morning.json"
            afternoon = root / "afternoon.json"
            morning.write_text('{"state":"done"}\n', encoding="utf-8")
            afternoon.write_text(
                '{"state":"complete"}\n',
                encoding="utf-8",
            )
            certificate: dict[str, object] = {
                "schema_version": 1,
                "passed": True,
                "evidence": {
                    "morning_journal_name": morning.name,
                    "morning_journal_sha256": hashlib.sha256(
                        morning.read_bytes()
                    ).hexdigest(),
                    "afternoon_journal_name": afternoon.name,
                    "afternoon_journal_sha256": hashlib.sha256(
                        afternoon.read_bytes()
                    ).hexdigest(),
                },
            }
            certificate["signature_hmac_sha256"] = _sign_payload(
                certificate,
                _load_signing_key(key_path),
            )
            _verify_signature(certificate, key_path)
            _verify_evidence(certificate, root)

            certificate["passed"] = False
            with self.assertRaisesRegex(
                RuntimeError,
                "signature",
            ):
                _verify_signature(certificate, key_path)
            certificate["passed"] = True
            morning.write_text('{"state":"changed"}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError,
                "hash mismatch",
            ):
                _verify_evidence(certificate, root)


if __name__ == "__main__":
    unittest.main()
