from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dev_simulation_certificate import (  # noqa: E402
    _sign_payload,
    validate_simulation_certificate,
)


class DevSimulationCertificateTests(unittest.TestCase):
    def _certificate(self, directory: str) -> tuple[Path, Path, dict[str, object]]:
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
        key = bytes.fromhex("ab" * 32)
        certificate: dict[str, object] = {
            "schema_version": 3,
            "environment": "simulation",
            "passed": True,
            "transition_spec_sha256": "t" * 64,
            "execution_source_sha256": "s" * 64,
            "xtquant_runtime_sha256": "r" * 64,
        }
        certificate["signature_hmac_sha256"] = _sign_payload(
            certificate,
            key,
        )
        certificate_path = root / "certificate.json"
        certificate_path.write_text(
            json.dumps(certificate),
            encoding="utf-8",
        )
        return certificate_path, key_path, certificate

    def test_matching_certificate_passes(self):
        with TemporaryDirectory() as directory:
            certificate_path, key_path, _ = self._certificate(directory)
            from unittest import mock

            with mock.patch(
                "dev_simulation_certificate.verify_state_machines",
                return_value={
                    "transition_spec_sha256": "t" * 64,
                    "execution_source_sha256": "s" * 64,
                },
            ), mock.patch(
                "dev_simulation_certificate.xtquant_runtime_sha256",
                return_value="r" * 64,
            ):
                valid, failures = validate_simulation_certificate(
                    certificate_path=certificate_path,
                    signing_key=key_path,
                )
            self.assertTrue(valid)
            self.assertEqual(failures, [])

    def test_stale_certificate_is_reported(self):
        with TemporaryDirectory() as directory:
            certificate_path, key_path, certificate = self._certificate(
                directory
            )
            certificate["execution_source_sha256"] = "changed"
            certificate_path.write_text(
                json.dumps(certificate),
                encoding="utf-8",
            )
            from unittest import mock

            with mock.patch(
                "dev_simulation_certificate.verify_state_machines",
                return_value={
                    "transition_spec_sha256": "t" * 64,
                    "execution_source_sha256": "current",
                },
            ), mock.patch(
                "dev_simulation_certificate.xtquant_runtime_sha256",
                return_value="r" * 64,
            ):
                valid, failures = validate_simulation_certificate(
                    certificate_path=certificate_path,
                    signing_key=key_path,
                )
            self.assertFalse(valid)
            self.assertTrue(
                any("execution sources changed" in f for f in failures)
            )

    def test_tampered_signature_is_reported(self):
        with TemporaryDirectory() as directory:
            certificate_path, key_path, certificate = self._certificate(
                directory
            )
            certificate["passed"] = False
            certificate_path.write_text(
                json.dumps(certificate),
                encoding="utf-8",
            )
            valid, failures = validate_simulation_certificate(
                certificate_path=certificate_path,
                signing_key=key_path,
            )
            self.assertFalse(valid)
            self.assertTrue(
                any("signature" in f for f in failures)
                or any("did not pass" in f for f in failures)
            )


if __name__ == "__main__":
    unittest.main()
