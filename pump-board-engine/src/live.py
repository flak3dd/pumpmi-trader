"""LIVE EXECUTION STUB — refuses by design.
Where Phantom would plug in (future, operator-approved per trade):
  - Grok Bot Phantom MCP (user-Phantom-mcp) swap/transfer tools, OR
  - pump.fun web UI in the box browser with Phantom extension confirmations.
This module never holds keys and never signs. Each live order must be confirmed by Simran."""
import os
class LiveRefused(RuntimeError): pass
def execute_live(order, cfg):
    if cfg.get("mode") != "live" or os.environ.get("PUMP_ENGINE_ENABLE_LIVE", "false").lower() != "true":
        raise LiveRefused("live disabled: needs mode: live AND PUMP_ENGINE_ENABLE_LIVE=true")
    raise LiveRefused("no wallet executor wired; route order to Grok Bot Phantom flow for manual confirm: %r" % (order,))
