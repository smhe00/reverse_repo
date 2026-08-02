from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from repo_live_readonly_preflight import (
    READ_ONLY_TRADER_METHODS,
    ReadOnlyTraderProxy,
    ReadOnlyViolation,
)


class FakeTrader:
    def connect(self) -> int:
        return 0

    def order_stock(self) -> int:
        raise AssertionError("the raw trading method must never be reached")

    def query_stock_order(self) -> object:
        return object()

    def cancel_order_stock(self) -> int:
        raise AssertionError("the raw cancel method must never be reached")


class ReadOnlyTraderProxyTests(unittest.TestCase):
    def test_allows_and_records_allowlisted_query(self) -> None:
        proxy = ReadOnlyTraderProxy(FakeTrader())

        self.assertEqual(proxy.connect(), 0)
        self.assertIsNotNone(proxy.query_stock_order())
        self.assertEqual(
            proxy.accessed_methods,
            ["connect", "query_stock_order"],
        )

    def test_blocks_every_non_allowlisted_method(self) -> None:
        for method_name in (
            "order_stock",
            "cancel_order_stock",
            "fund_transfer",
        ):
            with self.subTest(method_name=method_name):
                proxy = ReadOnlyTraderProxy(FakeTrader())

                with self.assertRaisesRegex(
                    ReadOnlyViolation,
                    "not read-only allowlisted",
                ):
                    getattr(proxy, method_name)

                self.assertNotIn(method_name, READ_ONLY_TRADER_METHODS)
