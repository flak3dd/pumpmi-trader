#!/usr/bin/env python3
"""Cache unit checks with no database connection or profitability claims."""
import sys
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pump-board-engine"))
from src.optimizer.store import DatabaseStore

class StateCacheTests(unittest.TestCase):
    def store(self):
        store = object.__new__(DatabaseStore)
        store.dialect = "mysql"
        store._lock = threading.RLock()
        store._state_cache = {}
        store.transaction = Mock(side_effect=lambda: nullcontext(None))
        cursor = Mock()
        cursor.fetchone.return_value = {"value_json": '{"count":1}'}
        store._execute = Mock(return_value=cursor)
        return store
    def test_cached_values_are_copied_and_do_not_query_twice(self):
        store = self.store()
        first = store.get_state("health")
        first["count"] = 999
        self.assertEqual(store.get_state("health"), {"count":1})
        self.assertEqual(store._execute.call_count, 1)
    def test_failed_write_does_not_publish_phantom_state(self):
        store = self.store()
        store.get_state("health")
        store._execute.side_effect = RuntimeError("test database write failure")
        with self.assertRaises(RuntimeError): store.set_state("health", {"count":999})
        self.assertEqual(store.get_state("health"), {"count":1})
    def test_expired_value_is_read_again(self):
        store = self.store()
        store._state_cache["health"] = (0., {"count":999})
        self.assertEqual(store.get_state("health"), {"count":1})
        self.assertEqual(store._execute.call_count, 1)

if __name__ == "__main__": unittest.main(verbosity=2)
