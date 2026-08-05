from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from pathlib import Path

from repo_execution_core import xtquant_runtime_sha256
from repo_execution_state_machine import verify_state_machines


def _load_json(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is missing or unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return payload


def _load_signing_key(path: Path) -> bytes:
    payload = _load_json(path, "release-gate signing key")
    try:
        key = bytes.fromhex(str(payload["hmac_sha256_key_hex"]))
    except (KeyError, ValueError) as exc:
        raise RuntimeError("release-gate signing key is invalid") from exc
    if payload.get("version") != 1 or len(key) != 32:
        raise RuntimeError("release-gate signing key is invalid")
    return key


def _sign_payload(payload: dict[str, object], key: bytes) -> str:
    unsigned = {
        name: value
        for name, value in payload.items()
        if name != "signature_hmac_sha256"
    }
    message = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def validate_simulation_certificate(
    *,
    certificate_path: Path,
    signing_key: Path,
) -> tuple[bool, list[str]]:
    certificate = _load_json(certificate_path, "simulation certificate")
    failures: list[str] = []
    if certificate.get("schema_version") != 3:
        failures.append("unsupported simulation certificate schema")
    if certificate.get("environment") != "simulation":
        failures.append("certificate is not from a simulation account")
    if certificate.get("passed") is not True:
        failures.append("certificate did not pass")
    provided = str(certificate.get("signature_hmac_sha256", ""))
    expected = _sign_payload(certificate, _load_signing_key(signing_key))
    if not hmac.compare_digest(provided, expected):
        failures.append("certificate signature is invalid")
    formal = verify_state_machines()
    if str(certificate.get("transition_spec_sha256", "")) != str(
        formal["transition_spec_sha256"]
    ):
        failures.append("state machines changed after certification")
    if str(certificate.get("execution_source_sha256", "")) != str(
        formal["execution_source_sha256"]
    ):
        failures.append("execution sources changed after certification")
    if str(certificate.get("xtquant_runtime_sha256", "")) != (
        xtquant_runtime_sha256()
    ):
        failures.append("XtQuant runtime changed after certification")
    return not failures, failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Developer-only read-only validation of the simulation "
            "validation certificate."
        )
    )
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--signing-key", required=True)
    args = parser.parse_args()
    valid, failures = validate_simulation_certificate(
        certificate_path=Path(args.certificate),
        signing_key=Path(args.signing_key),
    )
    if valid:
        print("Simulation validation certificate matches the current code.")
        return 0
    print("Simulation validation certificate is stale or invalid:")
    for failure in failures:
        print(f"  - {failure}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
