"""Durable, paper-only optimizer for fresh pump.fun observations.

This package deliberately does not share the fixture simulation sources or the
legacy file-backed live feed.  It is safe to import in the HTTP process: the
worker is started explicitly by ``scripts/serve.py`` only when enabled.
"""
from .runtime import OptimizerRuntime, Worker
from .store import DatabaseStore, StoreUnavailable

__all__ = ["DatabaseStore", "OptimizerRuntime", "StoreUnavailable", "Worker"]
