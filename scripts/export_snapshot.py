#!/usr/bin/env python3
"""Write the truthful static fallback consumed from public/data/snapshot.json."""
from __future__ import annotations

import json
import os
from pathlib import Path

from serve import ROOT, snapshot_payload


def main() -> None:
    destination = ROOT / "public" / "data" / "snapshot.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = snapshot_payload(simulation_available=False)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    print(f"wrote {destination}")


if __name__ == "__main__":
    main()
