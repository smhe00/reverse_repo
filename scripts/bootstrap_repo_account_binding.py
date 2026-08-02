from __future__ import annotations

import argparse
import random
from datetime import datetime
from pathlib import Path

from repo_execution_core import (
    AccountBindingError,
    account_id_fingerprint,
    atomic_write_json,
    qmt_path_fingerprint,
    strict_query,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a local reverse-repo account binding using hashes only. "
            "The plaintext account ID is never written or printed."
        )
    )
    parser.add_argument("--qmt-path", required=True)
    parser.add_argument(
        "--environment",
        required=True,
        choices=("live", "simulation"),
    )
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    qmt_path = Path(args.qmt_path).resolve()
    output_path = Path(args.output).resolve()
    if not qmt_path.is_dir():
        raise AccountBindingError(
            f"QMT userdata path does not exist: {qmt_path}"
        )
    if args.environment == "simulation" and "模拟" not in str(qmt_path):
        raise AccountBindingError(
            "simulation binding requires a simulation QMT path"
        )
    if args.environment == "live" and "模拟" in str(qmt_path):
        raise AccountBindingError(
            "live binding cannot use a simulation QMT path"
        )
    label = str(args.label).strip()
    if not label:
        raise AccountBindingError("binding label cannot be empty")

    from xtquant import xtconstant
    from xtquant.xttrader import XtQuantTrader

    trader = XtQuantTrader(
        str(qmt_path),
        random.randint(100_000_000, 999_999_999),
    )
    trader.start()
    try:
        result = int(trader.connect())
        if result != 0:
            raise AccountBindingError(
                f"QMT connection failed: {result}"
            )
        infos = list(
            strict_query(
                trader.query_account_infos,
                name="query_account_infos",
            )
        )
        statuses = list(
            strict_query(
                trader.query_account_status,
                name="query_account_status",
            )
        )
        normal_ids = {
            str(getattr(status, "account_id", "")).strip()
            for status in statuses
            if int(getattr(status, "account_type", -1))
            == int(xtconstant.SECURITY_ACCOUNT)
            and int(getattr(status, "status", -1))
            == int(xtconstant.ACCOUNT_STATUS_OK)
        }
        candidates = [
            str(getattr(info, "account_id", "")).strip()
            for info in infos
            if int(getattr(info, "account_type", -1))
            == int(xtconstant.SECURITY_ACCOUNT)
            and str(getattr(info, "account_id", "")).strip()
            in normal_ids
        ]
        if len(candidates) != 1:
            raise AccountBindingError(
                "expected exactly one normal securities account"
            )
        payload = {
            "version": 2,
            "created_at": datetime.now().astimezone().isoformat(),
            "accounts": [
                {
                    "label": label,
                    "environment": args.environment,
                    "account_type": "SECURITY_ACCOUNT",
                    "account_id_fingerprint": (
                        account_id_fingerprint(candidates[0])
                    ),
                    "qmt_path_fingerprint": qmt_path_fingerprint(
                        qmt_path
                    ),
                }
            ],
        }
        atomic_write_json(output_path, payload)
        print(
            "Created hashed account binding for "
            f"{args.environment!r} at {output_path}"
        )
        return 0
    finally:
        trader.stop()


if __name__ == "__main__":
    raise SystemExit(main())
