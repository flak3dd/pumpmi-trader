#!/usr/bin/env python3
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rpc_credential import seal, unseal
import run_cloud_worker

URL = "https://provider.invalid/?api-key=dummy-test-value"
DB = "mysql://dummy:test-only@database.invalid/test"


class CredentialTests(unittest.TestCase):
    def test_round_trip_without_plaintext_storage(self):
        blob = seal(URL, DB)
        self.assertNotIn(URL.encode(), blob)
        self.assertEqual(unseal(blob, DB), URL)

    def test_tampered_payload_and_wrong_database_key_are_rejected(self):
        blob = seal(URL, DB)
        for bad, key in ((blob[:-1] + bytes([blob[-1] ^ 1]), DB), (blob, DB + "wrong")):
            with self.subTest(key_matches=key == DB), self.assertRaises(Exception):
                unseal(bad, key)

    def test_non_https_and_missing_database_key_are_rejected(self):
        for url, db in (("http://provider.invalid", DB), (URL, "")):
            with self.assertRaises(ValueError):
                seal(url, db)

    def test_launcher_loads_only_into_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "rpc.bin"
            p.write_bytes(seal(URL, DB))
            with patch.dict(os.environ, {"PUMPMI_RPC_CREDENTIAL_FILE": str(p), "DATABASE_URL": DB}, clear=True), patch.object(run_cloud_worker.serve, "main") as start:
                run_cloud_worker.main()
                self.assertEqual(os.environ["SOLANA_RPC_URL"], URL)
                start.assert_called_once()
            self.assertNotIn(URL.encode(), p.read_bytes())

    def test_launcher_never_falls_back_to_an_old_endpoint(self):
        with patch.dict(os.environ, {"PUMPMI_RPC_CREDENTIAL_FILE": "/missing-rpc-credential", "SOLANA_RPC_URL": URL}, clear=True), patch.object(run_cloud_worker.serve, "main") as start:
            with self.assertRaises(SystemExit) as caught:
                run_cloud_worker.main()
            self.assertNotIn(URL, str(caught.exception))
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
