#!/usr/bin/env python3
"""Additive migration for the durable paper optimizer.

Usage (production):
    DATABASE_URL='mysql://...' python3 scripts/migrate_optimizer.py

This creates only ``optimizer_*`` tables and seeds the current strategy document
only when the durable optimizer namespace has no saved configuration. It never
writes ``pump-board-engine/strategy.json`` or any existing out dataset.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "pump-board-engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from src.optimizer.store import DatabaseStore, StoreUnavailable  # noqa: E402
from src.strategy_config import load_strategy_config  # noqa: E402


def main() -> None:
    try:
        store = DatabaseStore.from_env()
        seeded = store.migrate(load_strategy_config())
    except StoreUnavailable as exc:
        raise SystemExit(f"optimizer migration not run: {exc}") from exc
    print("optimizer migration complete; durable strategy seed=" + ("created" if seeded else "preserved"), flush=True)


if __name__ == "__main__":
    main()
