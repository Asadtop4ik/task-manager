"""Optional PostgreSQL verification for the checked-in synthetic QA fixture."""

from __future__ import annotations

import os
import unittest
from decimal import Decimal

from diagnostic_host import DiagnosticHost

QA_DATABASE_URL = os.environ.get("KETOSHOP_DIAGNOSTICS_TEST_DATABASE_URL", "")


@unittest.skipUnless(
    QA_DATABASE_URL,
    "set KETOSHOP_DIAGNOSTICS_TEST_DATABASE_URL to run SQL fixture checks",
)
class DiagnosticViewFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise unittest.SkipTest(
                "psycopg is required for database fixture checks"
            ) from exc
        cls.psycopg = psycopg
        cls.dict_row_factory = staticmethod(dict_row)

    def test_anonymized_views_and_large_finance_aggregate(self) -> None:
        with (
            self.psycopg.connect(
                QA_DATABASE_URL,
                connect_timeout=3,
                row_factory=self.__class__.dict_row_factory,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("SELECT current_database() AS name")
            self.assertEqual(cursor.fetchone()["name"], "ketoshop_diagnostics_qa")
            cursor.execute("""
                SELECT COUNT(*) AS orders,
                       SUM(revenue)::numeric AS revenue,
                       SUM(current_catalog_cost)::numeric AS current_cost,
                       SUM(missing_cost_items)::integer AS missing_cost_items,
                       BOOL_AND(cost_basis = 'current_catalog') AS labeled_estimate,
                       BOOL_AND(NOT historical_cost_data_available) AS no_history_claim
                FROM public.ketoshop_diag_finance_orders
                """)
            totals = cursor.fetchone()
            cursor.execute("""
                    SELECT has_table_privilege(
                               'ketoshop_diagnostics', 'public.orders', 'SELECT'
                           ) AS raw_orders,
                           has_table_privilege(
                               'ketoshop_diagnostics', 'public.products', 'SELECT'
                           ) AS raw_products,
                           has_table_privilege(
                               'ketoshop_diagnostics', 'public.expenses', 'SELECT'
                           ) AS raw_expenses,
                       has_table_privilege(
                           'ketoshop_diagnostics',
                           'public.ketoshop_diag_orders', 'SELECT'
                       ) AS safe_view
                """)
            grants = cursor.fetchone()
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name IN ('ketoshop_diag_orders', 'ketoshop_diag_order_items')
                """)
            exposed = {row["column_name"] for row in cursor.fetchall()}

        self.assertEqual(totals["orders"], 205)
        self.assertEqual(totals["revenue"], Decimal("10406250.00"))
        self.assertGreater(totals["current_cost"], 0)
        self.assertEqual(totals["missing_cost_items"], 12)
        self.assertTrue(totals["labeled_estimate"])
        self.assertTrue(totals["no_history_claim"])
        self.assertFalse(grants["raw_orders"])
        self.assertFalse(grants["raw_products"])
        self.assertFalse(grants["raw_expenses"])
        self.assertTrue(grants["safe_view"])
        self.assertNotIn("phone", exposed)
        self.assertNotIn("address", exposed)
        self.assertNotIn("customer_name", exposed)

    def test_host_aggregate_spans_more_than_200_order_rows(self) -> None:
        host = DiagnosticHost(
            intake_token="synthetic-test-token",
            database_url=QA_DATABASE_URL,
        )
        result, _digest, row_count = host._finance_summary(
            {"period": "day", "count": 7}
        )
        self.assertEqual(row_count, 7)
        self.assertEqual(
            sum(bucket["order_count"] for bucket in result["buckets"]), 205
        )
        self.assertEqual(
            sum(Decimal(bucket["revenue"]) for bucket in result["buckets"]),
            Decimal("10406250.00"),
        )


if __name__ == "__main__":
    unittest.main()
