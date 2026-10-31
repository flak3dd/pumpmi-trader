#!/usr/bin/env python3
"""Export a preserved archive as an isolated UI-test seed, never a live fallback."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    destination = ROOT / "scripts" / "fixtures" / "recorded-test-seed.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = ROOT / "pump-board-engine" / "out"
    board = json.loads((archive / "board.json").read_text(encoding="utf-8"))
    trades = json.loads((archive / "trade_charts.json").read_text(encoding="utf-8"))
    payload = {
        "board": board,
        "trades": trades,
        "meta": {
            "kind": "snapshot",
            "label": "Preserved archive for isolated software tests; not live market evidence",
            "updated": board.get("updated"),
            "simulationAvailable": False,
        },
    }
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    print(f"wrote isolated test archive: {destination}")


if __name__ == "__main__":
    main()
