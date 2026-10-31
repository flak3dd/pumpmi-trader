#!/usr/bin/env python3
"""Local stream software tests; no market, provider, or production database calls."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serve
from test_api import running_file_worker, write_board

class LiveStreamTests(unittest.TestCase):
    def test_stream_pushes_verified_frame_then_explicit_source_error(self):
        frame = {"board": {"ts":1, "columns":{"checking":[],"holding":[],"done":[]}}, "trades":{}, "meta":{"kind":"live","label":"Software test only"}}
        runtime = Mock(enabled=True)
        with patch.object(serve,"_runtime_for_payload",return_value=runtime), patch.object(serve,"live_feed_payload",side_effect=[frame,serve.ApiError(503,"test outage")]), patch.object(serve,"_read_json",side_effect=AssertionError("historical fallback")), patch("serve.time.sleep",return_value=None):
            server = ThreadingHTTPServer(("127.0.0.1",0),serve.PumpmiHandler)
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/live-feed/events",timeout=5) as response:
                    self.assertIn("text/event-stream",response.headers["Content-Type"])
                    lines = []
                    for _ in range(12):
                        line = response.readline().decode().strip()
                        lines.append(line)
                        if line.startswith("data:") and "disconnected" in line: break
                    self.assertIn("event: feed",lines)
                    self.assertIn("event: feed-error",lines)
                    payload = next(line[6:] for line in lines if line.startswith("data:") and '"board"' in line)
                    self.assertEqual(json.loads(payload)["meta"]["kind"],"live")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def _serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.PumpmiHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_stream_opens_for_a_live_file_worker_without_optimizer(self):
        frame = {
            "board": {"ts": 1, "bankroll": 1500, "equity": 1108.36, "columns": {"checking": [], "holding": [], "done": []}},
            "trades": {},
            "meta": {"kind": "live", "label": "Verified live-market paper board", "simulationAvailable": False},
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write_board(out, now=time.time(), ts=time.time())
            with running_file_worker(out), patch.dict(os.environ, {"DATABASE_URL": ""}), patch.object(serve, "OUT_DIR", out), patch.object(
                serve, "live_feed_payload", side_effect=[frame, frame, serve.ApiError(503, "worker stopped")]
            ), patch.object(serve, "_read_json", side_effect=AssertionError("historical fallback")), patch("serve.time.sleep", return_value=None):
                server, thread = self._serve()
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/live-feed/events", timeout=5) as response:
                        self.assertIn("text/event-stream", response.headers["Content-Type"])
                        lines = []
                        for _ in range(12):
                            line = response.readline().decode().strip()
                            lines.append(line)
                            if line.startswith("data:") and "disconnected" in line:
                                break
                        self.assertIn("event: feed", lines)
                        self.assertIn("event: feed-error", lines)
                        payload = next(line[6:] for line in lines if line.startswith("data:") and '"board"' in line)
                        self.assertEqual(json.loads(payload)["meta"]["kind"], "live")
                        self.assertEqual(json.loads(payload)["board"]["equity"], 1108.36)
                        self.assertIn(": heartbeat", lines)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_stream_stays_closed_without_optimizer_or_file_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write_board(out, now=time.time(), ts=time.time(), equity=424242.42)
            with patch.dict(os.environ, {"DATABASE_URL": ""}), patch.object(serve, "OUT_DIR", out), patch.object(
                serve, "live_feed_payload", side_effect=AssertionError("stream body ran")
            ), patch.object(serve, "_read_json", side_effect=AssertionError("historical fallback")):
                server, thread = self._serve()
                try:
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/live-feed/events", timeout=5)
                    self.assertEqual(caught.exception.code, 503)
                    body = json.loads(caught.exception.read().decode())
                    self.assertIn("not running", body["error"])
                    self.assertIn("No historical fallback", body["error"])
                    self.assertNotIn("424242.42", body["error"])
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

if __name__ == "__main__": unittest.main(verbosity=2)
