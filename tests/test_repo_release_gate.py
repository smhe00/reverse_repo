from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_repo_release_gate import main as release_gate_main  # noqa: E402


class ReleaseGateLiveOnlyTests(unittest.TestCase):
    def _arguments(self, directory: str) -> list[str]:
        root = Path(directory)
        return [
            "verify_repo_release_gate.py",
            "--qmt-path", str(root / "qmt"),
            "--account-binding", str(root / "binding.json"),
            "--live-channel-certificate", str(root / "live.json"),
            "--signing-key", str(root / "key.json"),
            "--strategy-config", str(root / "runtime.json"),
        ]

    def test_gate_passes_with_valid_live_channel_certificate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "live.json").write_text("{}", encoding="utf-8")
            binding = type(
                "Binding", (), {"qmt_path_fingerprint": "path"}
            )()
            verification = {
                "transition_spec_sha256": "transition",
                "execution_source_sha256": "source",
            }
            with mock.patch("sys.argv", self._arguments(directory)), mock.patch(
                "verify_repo_release_gate.load_account_binding",
                return_value=binding,
            ) as binding_load, mock.patch(
                "verify_repo_release_gate.verify_state_machines",
                return_value=verification,
            ), mock.patch(
                "verify_repo_release_gate.verify_live_channel_certificate"
            ) as live, mock.patch(
                "verify_repo_release_gate.reverse_repo_strategy_config_sha256"
            ):
                self.assertEqual(release_gate_main(), 0)
                live.assert_called_once()
                self.assertEqual(
                    binding_load.call_args.kwargs["environment"],
                    "live",
                )

    def test_gate_fails_when_live_certificate_is_invalid(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "live.json").write_text("{}", encoding="utf-8")
            binding = type(
                "Binding", (), {"qmt_path_fingerprint": "path"}
            )()
            verification = {
                "transition_spec_sha256": "transition",
                "execution_source_sha256": "source",
            }
            with mock.patch("sys.argv", self._arguments(directory)), mock.patch(
                "verify_repo_release_gate.load_account_binding",
                return_value=binding,
            ), mock.patch(
                "verify_repo_release_gate.verify_state_machines",
                return_value=verification,
            ), mock.patch(
                "verify_repo_release_gate.verify_live_channel_certificate",
                side_effect=RuntimeError("stale certificate"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "实盘启用门禁被拒绝",
                ):
                    release_gate_main()

    def test_gate_requires_a_path_bound_live_account(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "live.json").write_text("{}", encoding="utf-8")
            binding = type(
                "Binding", (), {"qmt_path_fingerprint": None}
            )()
            with mock.patch("sys.argv", self._arguments(directory)), mock.patch(
                "verify_repo_release_gate.load_account_binding",
                return_value=binding,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "does not bind the QMT path",
                ):
                    release_gate_main()

    def test_gate_requires_the_live_certificate_argument(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = [
                "verify_repo_release_gate.py",
                "--qmt-path", str(root / "qmt"),
                "--account-binding", str(root / "binding.json"),
                "--signing-key", str(root / "key.json"),
                "--strategy-config", str(root / "runtime.json"),
            ]
            with mock.patch("sys.argv", arguments):
                with self.assertRaises(SystemExit) as raised:
                    release_gate_main()
                self.assertEqual(raised.exception.code, 2)

    def test_gate_certificate_payload_must_be_valid_json(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "live.json").write_text(
                "not-json",
                encoding="utf-8",
            )
            binding = type(
                "Binding", (), {"qmt_path_fingerprint": "path"}
            )()
            verification = {
                "transition_spec_sha256": "transition",
                "execution_source_sha256": "source",
            }
            with mock.patch("sys.argv", self._arguments(directory)), mock.patch(
                "verify_repo_release_gate.load_account_binding",
                return_value=binding,
            ), mock.patch(
                "verify_repo_release_gate.verify_state_machines",
                return_value=verification,
            ), mock.patch(
                "verify_repo_release_gate.verify_live_channel_certificate"
            ) as live:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "实盘启用门禁被拒绝",
                ):
                    release_gate_main()
                live.assert_not_called()


if __name__ == "__main__":
    unittest.main()
