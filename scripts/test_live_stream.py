#!/usr/bin/env python3
"""Local stream software tests; no market, provider, or production database calls."""
import json
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serve

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

if __name__ == "__main__": unittest.main(verbosity=2)
