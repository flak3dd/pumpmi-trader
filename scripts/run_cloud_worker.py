#!/usr/bin/env python3
"""Cloud-only launcher: no plaintext RPC credential file or log output."""
import os
from pathlib import Path

import serve
from rpc_credential import unseal


def main() -> None:
    path = os.environ.get("PUMPMI_RPC_CREDENTIAL_FILE")
    if not path:
        raise SystemExit("Protected RPC credential file is not configured")
    try:
        endpoint = unseal(Path(path).read_bytes(), os.environ.get("DATABASE_URL", ""))
    except Exception:
        raise SystemExit("Protected RPC credential cannot be loaded; refusing an unsafe fallback") from None
    os.environ["SOLANA_RPC_URL"] = endpoint
    serve.main()


if __name__ == "__main__":
    main()
