from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from repo_execution_core import atomic_write_json  # noqa: E402
from repo_live_enable_manifest import (  # noqa: E402
    create_live_enable_manifest,
    verify_live_enable_manifest,
)


VERIFICATION = {
    "transition_spec_sha256": "1" * 64,
    "execution_source_sha256": "2" * 64,
}
RUNTIME_HASH = "3" * 64


class LiveEnableManifestTests(unittest.TestCase):
    def _files(self, directory: str) -> tuple[Path, Path, Path, Path]:
        root = Path(directory)
        config = root / "runtime.json"
        certificate = root / "certificate.json"
        key = root / "key.json"
        manifest = root / "manifest.json"
        config.write_text(
            json.dumps(
                {
                    "first_execution_time": "09:30:42",
                    "second_execution_time": "15:10:00",
                    "first_cash_usage_ratio": 0.90,
                    "second_cash_usage_ratio": 1.0,
                }
            ),
            encoding="utf-8",
        )
        certificate.write_text('{"passed":true}', encoding="utf-8")
        key.write_text(
            json.dumps(
                {
                    "version": 1,
                    "hmac_sha256_key_hex": "ab" * 32,
                }
            ),
            encoding="utf-8",
        )
        return config, certificate, key, manifest

    def _create(self, directory: str) -> tuple[Path, Path, Path, Path]:
        config, certificate, key, manifest = self._files(directory)
        payload = create_live_enable_manifest(
            strategy_config=config,
            live_channel_certificate=certificate,
            signing_key=key,
            now=datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc),
            verification=VERIFICATION,
            runtime_sha256=RUNTIME_HASH,
        )
        atomic_write_json(manifest, payload)
        return config, certificate, key, manifest

    def test_matching_manifest_passes(self):
        with TemporaryDirectory() as directory:
            config, certificate, key, manifest = self._create(directory)
            verified = verify_live_enable_manifest(
                manifest_path=manifest,
                strategy_config=config,
                live_channel_certificate=certificate,
                signing_key=key,
                verification=VERIFICATION,
                runtime_sha256=RUNTIME_HASH,
            )
            self.assertEqual(verified["strategy_config"]["first_cash_usage_ratio"], 0.9)
            self.assertEqual(verified["schema_version"], 2)

    def test_valid_ratio_change_after_enable_is_rejected(self):
        with TemporaryDirectory() as directory:
            config, certificate, key, manifest = self._create(directory)
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["first_cash_usage_ratio"] = 0.80
            config.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed after rr on"):
                verify_live_enable_manifest(
                    manifest_path=manifest,
                    strategy_config=config,
                    live_channel_certificate=certificate,
                    signing_key=key,
                    verification=VERIFICATION,
                    runtime_sha256=RUNTIME_HASH,
                )

    def test_invalid_ratio_is_rejected(self):
        with TemporaryDirectory() as directory:
            config, certificate, key, manifest = self._create(directory)
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["second_cash_usage_ratio"] = 1.01
            config.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "from 0 through 1"):
                verify_live_enable_manifest(
                    manifest_path=manifest,
                    strategy_config=config,
                    live_channel_certificate=certificate,
                    signing_key=key,
                    verification=VERIFICATION,
                    runtime_sha256=RUNTIME_HASH,
                )

    def test_certificate_or_manifest_tampering_is_rejected(self):
        with TemporaryDirectory() as directory:
            config, certificate, key, manifest = self._create(directory)
            certificate.write_text('{"passed":false}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "certificate changed"):
                verify_live_enable_manifest(
                    manifest_path=manifest,
                    strategy_config=config,
                    live_channel_certificate=certificate,
                    signing_key=key,
                    verification=VERIFICATION,
                    runtime_sha256=RUNTIME_HASH,
                )
            certificate.write_text('{"passed":true}', encoding="utf-8")
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["strategy_config"]["first_cash_usage_ratio"] = 0.5
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "signature"):
                verify_live_enable_manifest(
                    manifest_path=manifest,
                    strategy_config=config,
                    live_channel_certificate=certificate,
                    signing_key=key,
                    verification=VERIFICATION,
                    runtime_sha256=RUNTIME_HASH,
                )

    def test_source_or_xtquant_change_after_enable_is_rejected(self):
        with TemporaryDirectory() as directory:
            config, certificate, key, manifest = self._create(directory)
            changed = dict(VERIFICATION)
            changed["execution_source_sha256"] = "4" * 64
            with self.assertRaisesRegex(RuntimeError, "sources changed"):
                verify_live_enable_manifest(
                    manifest_path=manifest,
                    strategy_config=config,
                    live_channel_certificate=certificate,
                    signing_key=key,
                    verification=changed,
                    runtime_sha256=RUNTIME_HASH,
                )
            with self.assertRaisesRegex(RuntimeError, "XtQuant runtime changed"):
                verify_live_enable_manifest(
                    manifest_path=manifest,
                    strategy_config=config,
                    live_channel_certificate=certificate,
                    signing_key=key,
                    verification=VERIFICATION,
                    runtime_sha256="5" * 64,
                )

    def test_execution_source_commit_change_after_enable_is_rejected(self):
        with TemporaryDirectory() as directory:
            config, certificate, key, manifest = self._files(directory)
            armed = dict(VERIFICATION)
            armed["execution_source_commit"] = "a" * 40
            payload = create_live_enable_manifest(
                strategy_config=config,
                live_channel_certificate=certificate,
                signing_key=key,
                now=datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc),
                verification=armed,
                runtime_sha256=RUNTIME_HASH,
            )
            atomic_write_json(manifest, payload)
            moved = dict(VERIFICATION)
            moved["execution_source_commit"] = "b" * 40
            with self.assertRaisesRegex(
                RuntimeError,
                "commit changed",
            ):
                verify_live_enable_manifest(
                    manifest_path=manifest,
                    strategy_config=config,
                    live_channel_certificate=certificate,
                    signing_key=key,
                    verification=moved,
                    runtime_sha256=RUNTIME_HASH,
                )

    def test_live_wrappers_verify_manifest_before_resolving_qmt(self):
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        for name in (
            "run_gc001_daily_90pct_093042.ps1",
            "run_gc001_r001_afternoon_sweep.ps1",
        ):
            with self.subTest(name=name):
                source = (scripts / name).read_text(encoding="utf-8")
                manifest_check = source.index(
                    "Assert-ReverseRepoLiveEnableManifest"
                )
                qmt_resolution = source.index(
                    "Get-ReverseRepoLiveQmtPath"
                )
                self.assertLess(manifest_check, qmt_resolution)


if __name__ == "__main__":
    unittest.main()
