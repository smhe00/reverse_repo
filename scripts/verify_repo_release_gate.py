from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from pathlib import Path

from repo_execution_core import (
    load_account_binding,
    reverse_repo_strategy_config_sha256,
    xtquant_runtime_sha256,
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
    parser.add_argument("--simulation-certificate", default="")
    parser.add_argument("--live-channel-certificate", default="")
    parser.add_argument("--signing-key", required=True)
    parser.add_argument("--strategy-config", required=True)
    args = parser.parse_args()

    verification = verify_state_machines()
    expected_hash = str(verification["transition_spec_sha256"])
    expected_source_hash = str(
        verification["execution_source_sha256"]
    )
    binding = load_account_binding(
        Path(args.account_binding),
        environment="live",
        qmt_path=Path(args.qmt_path),
    )
    if binding.qmt_path_fingerprint is None:
        raise RuntimeError("live binding does not bind the QMT path")
    errors: list[str] = []
    if args.simulation_certificate:
        try:
            _verify_simulation_certificate(
                certificate_path=Path(args.simulation_certificate),
                signing_key=Path(args.signing_key),
                strategy_config=Path(args.strategy_config),
                expected_hash=expected_hash,
                expected_source_hash=expected_source_hash,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"full simulation certificate invalid: {exc}")
        else:
            print(
                "Live enable gate passed using full simulation "
                f"certification for state-machine specification {expected_hash}"
            )
            print(
                "Certification basis: full simulation certification "
                "(normal, recovery, and afternoon paths)."
            )
            return 0
    if args.live_channel_certificate:
        try:
            verify_live_channel_certificate(
                certificate=json.loads(
                    Path(args.live_channel_certificate).read_text(encoding="utf-8")
                ),
                certificate_path=Path(args.live_channel_certificate),
                signing_key=Path(args.signing_key),
                qmt_path=Path(args.qmt_path),
                account_binding=Path(args.account_binding),
                expected_transition_hash=expected_hash,
                expected_source_hash=expected_source_hash,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"live-channel certificate invalid: {exc}")
        else:
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
    if not errors:
        errors.append("no certification certificate was provided")
    raise RuntimeError("live enable gate failed: " + " | ".join(errors))


def _verify_simulation_certificate(
    *,
    certificate_path: Path,
    signing_key: Path,
    strategy_config: Path,
    expected_hash: str,
    expected_source_hash: str,
) -> None:
    try:
        certificate = json.loads(
            certificate_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "simulation verification certificate is missing or unreadable"
        ) from exc
    if certificate.get("schema_version") != 3:
        raise RuntimeError(
            "unsupported simulation verification certificate"
        )
    _verify_signature(certificate, signing_key)
    _verify_evidence(certificate, certificate_path.parent)
    if certificate.get("passed") is not True:
        raise RuntimeError("simulation verification did not pass")
    if certificate.get("environment") != "simulation":
        raise RuntimeError("certificate is not from a simulation account")
    if certificate.get("transition_spec_sha256") != expected_hash:
        raise RuntimeError(
            "simulation certificate does not match the current state machines"
        )
    if certificate.get("execution_source_sha256") != expected_source_hash:
        raise RuntimeError(
            "simulation certificate does not match the current executors"
        )
    if (
        certificate.get("xtquant_runtime_sha256")
        != xtquant_runtime_sha256()
    ):
        raise RuntimeError(
            "simulation certificate does not match the installed XtQuant"
        )
    _verify_schedule_configuration(
        certificate,
        strategy_config,
    )
    _verify_certificate_timestamp(certificate)
    required_checks = {
        "simulation_path_bound",
        "account_bound",
        "connection_ok",
        "asset_query_ok",
        "order_query_ok",
        "quote_subscription_ok",
        "morning_normal_order_lifecycle_ok",
        "morning_normal_production_path_ok",
        "afternoon_normal_order_lifecycle_ok",
        "morning_fault_recovery_ok",
        "fault_injection_isolated",
        "fault_state_space_verified",
        "normal_schedule_paths_match_validation_plan",
        "evidence_files_isolated",
        "all_validation_orders_terminal",
        "all_validation_remarks_unique",
        "validation_namespaces_ok",
        "validation_order_identity_ok",
        "morning_normal_broker_evidence_ok",
        "afternoon_normal_broker_evidence_ok",
        "morning_recovery_broker_evidence_ok",
    }
    checks = certificate.get("checks")
    if not isinstance(checks, dict):
        raise RuntimeError("simulation certificate checks are missing")
    failed = sorted(
        name for name in required_checks if checks.get(name) is not True
    )
    if failed:
        raise RuntimeError(
            "simulation gate checks are incomplete: " + ", ".join(failed)
        )


def _verify_certificate_timestamp(
    certificate: dict[str, object],
    *,
    now: datetime | None = None,
) -> None:
    try:
        certified_at = datetime.fromisoformat(
            str(certificate["certified_at"])
        )
    except (KeyError, ValueError) as exc:
        raise RuntimeError(
            "simulation certificate timestamp is invalid"
        ) from exc
    if certified_at.utcoffset() is None:
        raise RuntimeError(
            "simulation certificate timestamp must include a timezone"
        )
    current = datetime.now().astimezone() if now is None else now
    if current.utcoffset() is None:
        raise ValueError("current gate time must include a timezone")
    age = current.astimezone() - certified_at.astimezone()
    if age < -timedelta(minutes=5):
        raise RuntimeError(
            "simulation certificate timestamp is unexpectedly in the future"
        )


def _verify_schedule_configuration(
    certificate: dict[str, object],
    strategy_config_path: Path,
) -> None:
    # The certificate proves executor capability. The signed live-enable
    # manifest separately locks all four current live parameters.
    reverse_repo_strategy_config_sha256(strategy_config_path)


def _verify_signature(
    certificate: dict[str, object],
    signing_key_path: Path,
) -> None:
    try:
        key_payload = json.loads(
            signing_key_path.read_text(encoding="utf-8")
        )
        key = bytes.fromhex(
            str(key_payload["hmac_sha256_key_hex"])
        )
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("release-gate signing key is unreadable") from exc
    if key_payload.get("version") != 1 or len(key) != 32:
        raise RuntimeError("release-gate signing key is invalid")
    provided = str(certificate.get("signature_hmac_sha256", ""))
    unsigned = {
        name: value
        for name, value in certificate.items()
        if name != "signature_hmac_sha256"
    }
    message = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    expected = hmac.new(key, message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided, expected):
        raise RuntimeError(
            "simulation certificate signature is invalid"
        )


def _verify_evidence(
    certificate: dict[str, object],
    directory: Path,
) -> None:
    evidence = certificate.get("evidence")
    if not isinstance(evidence, dict):
        raise RuntimeError("simulation evidence hashes are missing")
    for prefix in (
        "morning_normal",
        "afternoon_normal",
        "morning_recovery",
    ):
        name = str(evidence.get(f"{prefix}_journal_name", ""))
        if Path(name).name != name or not name:
            raise RuntimeError("simulation evidence filename is invalid")
        path = directory / name
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeError(
                f"simulation evidence is unreadable: {name}"
            ) from exc
        if actual != evidence.get(f"{prefix}_journal_sha256"):
            raise RuntimeError(
                f"simulation evidence hash mismatch: {name}"
            )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"{type(exc).__name__}: {exc}")
        raise SystemExit(1) from None
