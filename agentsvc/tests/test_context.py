from __future__ import annotations

import threading
import unittest

from agent_svc.context import CancelRegistry


class CancelRegistryTests(unittest.TestCase):
    def test_cancel_all_sets_every_registered_event(self) -> None:
        registry = CancelRegistry()
        a = threading.Event()
        b = threading.Event()
        registry.register(a)
        registry.register(b)
        registry.cancel_all()
        self.assertTrue(a.is_set())
        self.assertTrue(b.is_set())

    def test_unregistered_event_is_not_touched(self) -> None:
        registry = CancelRegistry()
        a = threading.Event()
        b = threading.Event()
        registry.register(a)
        registry.register(b)
        registry.unregister(b)
        registry.cancel_all()
        self.assertTrue(a.is_set())
        self.assertFalse(b.is_set())

    def test_unregistering_an_unknown_event_is_a_no_op(self) -> None:
        registry = CancelRegistry()
        registry.unregister(threading.Event())  # must not raise

    def test_cancel_all_with_nothing_registered_is_a_no_op(self) -> None:
        CancelRegistry().cancel_all()  # must not raise


if __name__ == "__main__":
    unittest.main()
