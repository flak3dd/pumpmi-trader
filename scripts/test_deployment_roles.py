#!/usr/bin/env python3
"""Isolated startup-role tests: no sockets, live DB, RPC or paper trades."""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pump-board-engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import serve
from src.optimizer import runtime as runtime_module


class DeploymentRoleTests(unittest.TestCase):
    def run_startup(self, setting):
        env = {"HOST": "127.0.0.1", "PORT": "9876"}
        if setting is not None:
            env["PUMPMI_WORKER_ENABLED"] = setting
        runtime = SimpleNamespace(enabled=True, boot_id="isolated-test")
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.dict(os.environ, env, clear=True), \
                patch.object(serve, "optimizer_runtime", return_value=runtime), \
                patch.object(serve, "ThreadingHTTPServer", return_value=server) as factory, \
                patch.object(runtime_module, "Worker") as worker, \
                contextlib.redirect_stdout(io.StringIO()):
            serve.main()
        factory.assert_called_once_with(("127.0.0.1", 9876), serve.PumpmiHandler)
        server.server_close.assert_called_once()
        return runtime, worker

    def test_dashboard_only_role_keeps_live_runtime_without_a_worker(self):
        runtime, worker = self.run_startup("0")
        self.assertTrue(runtime.enabled)
        worker.assert_not_called()

    def test_vm_role_starts_and_cleans_up_exactly_one_worker(self):
        runtime, worker = self.run_startup("1")
        worker.assert_called_once_with(runtime)
        worker.return_value.start.assert_called_once()
        worker.return_value.stop.assert_called_once()
        worker.return_value.join.assert_called_once_with(timeout=5)

    def test_missing_flag_preserves_legacy_explicit_worker_hosts(self):
        _, worker = self.run_startup(None)
        worker.return_value.start.assert_called_once()

    def test_false_and_invalid_flags_fail_closed(self):
        for setting in ("false", "no", "off", "", "invalid"):
            with self.subTest(setting=setting):
                _, worker = self.run_startup(setting)
                worker.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
