#!/usr/bin/env python3
"""Read-only HTTP readiness check; never send PUT/POST or open a database."""
import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:3000")
    parser.add_argument("--before", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--after-timestamp", type=float, default=0)
    args = parser.parse_args()

    def get(path: str) -> dict:
        print(f"Reading {path} (no mutation)", flush=True)
        with urllib.request.urlopen(args.origin.rstrip("/") + path, timeout=45) as response:
            return json.load(response)

    last_error = "readiness was not checked"
    for attempt in range(1, 7):
        try:
            assert get("/health")["ok"] is True
            strategy = get("/api/strategy")
            status = get("/api/optimizer")
            frame = get("/api/live-feed")
            assert strategy["paperOnly"] is True and strategy["strategy"]["max_open"] == 6
            assert status["enabled"] is True and status["paperOnly"] is True
            assert status["portfolio"]["openPositions"] <= 6
            assert frame["meta"]["kind"] in {"live", "awaiting", "stale"}
            if args.before:
                before = json.loads(args.before.read_text())
                assert strategy["revision"] == before["revision"]
                assert strategy["watchPeriods"] == before["periods"]
                assert status["portfolio"]["bankroll"] == before["portfolio"]["bankroll"]
            record = {
                "verifiedAt": time.time(), "health": "ok", "runtime": status["runtime"],
                "leaseOwned": status["leaseOwned"], "bootId": status["bootId"],
                "revision": strategy["revision"], "watchPeriods": strategy["watchPeriods"],
                "maxOpen": strategy["strategy"]["max_open"], "portfolio": status["portfolio"],
                "source": status["source"], "frameKind": frame["meta"]["kind"],
                "frameTimestamp": frame["board"].get("ts"),
                "boardStats": frame["board"]["stats"],
            }
            if args.output:
                args.output.write_text(json.dumps(record, indent=2) + "\n")
            # Status and frame are independent GETs; a new tick may commit between
            # them. Use the verified frame's own provenance for generation checks.
            observed = frame["board"].get("ts")
            new_tick = observed is not None and float(observed) >= args.after_timestamp
            if status["leaseOwned"] and frame["meta"]["kind"] == "live" and new_tick:
                print(json.dumps(record, indent=2), flush=True)
                return
            last_error = f"leaseOwned={status['leaseOwned']}, frame={frame['meta']['kind']}, newTick={new_tick}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = type(exc).__name__
        print(f"Attempt {attempt}: waiting for live worker ({last_error})", flush=True)
        time.sleep(8)
    raise RuntimeError(f"Live readiness not established: {last_error}")


if __name__ == "__main__":
    main()
