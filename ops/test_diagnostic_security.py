import unittest

from diagnostic_security import (
    QueryError,
    parse_select,
    redact_log_line,
    structured_log_metadata,
)


class DiagnosticSecurityTests(unittest.TestCase):
    def test_query_compiles_only_allowlisted_columns_with_parameters_and_bound(self):
        parsed = parse_select(
            "SELECT order_id, status FROM ketoshop_diag_orders "
            "WHERE order_id = 71001 ORDER BY created_at DESC LIMIT 12  "
        )
        self.assertEqual(parsed.params, (71001,))
        self.assertEqual(parsed.limit, 12)
        self.assertIn('FROM "ketoshop_diag_orders"', parsed.sql)
        self.assertTrue(parsed.sql.endswith("LIMIT 13"))
        self.assertNotIn("71001", parsed.sql)
        self.assertTrue(parsed.digest)

    def test_query_rejects_raw_tables_contacts_joins_and_mutations(self):
        for query in (
            "SELECT phone FROM orders",
            "SELECT address FROM ketoshop_diag_orders",
            "SELECT order_id FROM public.orders",
            "SELECT order_id FROM ketoshop_diag_orders; DROP TABLE orders",
            "SELECT order_id FROM ketoshop_diag_orders JOIN users ON true",
            "DELETE FROM ketoshop_diag_orders",
        ):
            with self.subTest(query=query), self.assertRaises(QueryError):
                parse_select(query)

    def test_query_rejects_more_than_200_rows(self):
        with self.assertRaisesRegex(QueryError, "between 1 and 200"):
            parse_select("SELECT * FROM ketoshop_diag_orders LIMIT 201")

    def test_logs_redact_customer_fields_contacts_and_tokens(self):
        line = (
            'phone=+998901234567 address="Synthetic Street" '
            "owner@example.com Bearer abcdefghijklmnopqrstuvwxyz "
            "123456:abcdefghijklmnopqrstuvwxyz0123456789"
        )
        safe = redact_log_line(line, ("host-secret",))
        self.assertNotIn("+998901234567", safe)
        self.assertNotIn("Synthetic Street", safe)
        self.assertNotIn("owner@example.com", safe)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", safe)
        unquoted_multiword = redact_log_line(
            "address=Unit 4, House 9 customer_name=Jane Example"
        )
        self.assertNotIn("House 9", unquoted_multiword)
        self.assertNotIn("Jane Example", unquoted_multiword)

    def test_log_metadata_drops_unstructured_text_and_unknown_event_values(self):
        self.assertIsNone(structured_log_metadata("ERROR: customer Alice Doe"))
        safe = structured_log_metadata(
            '{"level":"error","event":"customer Alice Doe",'
            '"message":"address Unit 4, House 9"}'
        )
        self.assertEqual(safe, {"level": "error"})


if __name__ == "__main__":
    unittest.main()
