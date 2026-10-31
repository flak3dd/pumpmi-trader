#!/usr/bin/env python3
"""Software checks only; never contact an RPC or use real provider credentials."""
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pump-board-engine"))
from src.optimizer.source import SourceError, _rpc_json

class RpcSettingsTests(unittest.TestCase):
    def test_private_endpoint_is_server_side_and_bounded(self):
        with patch.dict("os.environ", {"SOLANA_RPC_URL": "https://rpc.example.invalid/?key=TEST_ONLY"}), patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"result": {"value": {"uiAmount": 1}}}')) as request:
            _rpc_json({"jsonrpc":"2.0","id":1,"method":"getTokenSupply","params":["test-only-mint"]})
            self.assertEqual(request.call_args.args[0].full_url, "https://rpc.example.invalid/?key=TEST_ONLY")
            self.assertEqual(request.call_args.kwargs["timeout"], 4.0)
    def test_error_does_not_expose_endpoint_credentials(self):
        with patch.dict("os.environ", {"SOLANA_RPC_URL": "https://rpc.example.invalid/?key=TEST_ONLY"}), patch("urllib.request.urlopen", side_effect=urllib.error.URLError("TEST_ONLY")):
            with self.assertRaises(SourceError) as caught: _rpc_json({})
            self.assertNotIn("TEST_ONLY", str(caught.exception))
    def test_non_https_provider_is_rejected(self):
        with patch.dict("os.environ", {"SOLANA_RPC_URL": "http://rpc.example.invalid"}):
            with self.assertRaises(SourceError): _rpc_json({})

if __name__ == "__main__": unittest.main(verbosity=2)
