from __future__ import annotations

import argparse
import json
from pathlib import Path

from repo_execution_core import (
    load_account_binding,
    reverse_repo_strategy_config_sha256,
)
from repo_execution_state_machine import verify_state_machines
from repo_live_channel_validation import verify_live_channel_certificate


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed gate required before live scheduled tasks can "
            "be enabled."
        )
    )
    parser.add_argument("--qmt-path", required=True)
    parser.add_argument("--account-binding", required=True)
    parser.add_argument("--live-channel-certificate", required=True)
    parser.add_argument("--signing-key", required=True)
    parser.add_argument("--strategy-config", required=True)
    args = parser.parse_args()

    verification = verify_state_machines()
    expected_hash = str(verification["transition_spec_sha256"])
    expected_source_hash = str(verification["execution_source_sha256"])
    binding = load_account_binding(
        Path(args.account_binding),
        environment="live",
        qmt_path=Path(args.qmt_path),
    )
    if binding.qmt_path_fingerprint is None:
        raise RuntimeError("live binding does not bind the QMT path")
    try:
        verify_live_channel_certificate(
            certificate=json.loads(
                Path(args.live_channel_certificate).read_text(
                    encoding="utf-8"
                )
            ),
            certificate_path=Path(args.live_channel_certificate),
            signing_key=Path(args.signing_key),
            qmt_path=Path(args.qmt_path),
            account_binding=Path(args.account_binding),
            expected_transition_hash=expected_hash,
            expected_source_hash=expected_source_hash,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "实盘启用门禁被拒绝：实盘快速认证证书无效："
            f"{exc}"
        ) from exc
    reverse_repo_strategy_config_sha256(Path(args.strategy_config))
    print(
        "Live enable gate passed using CNY 1,000 live-channel "
        f"certification for state-machine specification {expected_hash}"
    )
    print(
        "Certification basis: live-channel certification; "
        "does not include fault-injection recovery proof."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"{type(exc).__name__}: {exc}")
        raise SystemExit(1) from None
