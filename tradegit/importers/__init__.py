"""Broker importer registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import generic, ibkr, schwab

REGISTRY = {
    "ibkr": ibkr,
    "schwab": schwab,
    "generic": generic,
}
ALIASES = {
    "ib": "ibkr", "interactivebrokers": "ibkr", "interactive-brokers": "ibkr",
    "盈透": "ibkr", "tws": "ibkr", "flex": "ibkr",
    "charles-schwab": "schwab", "charlesschwab": "schwab", "嘉信": "schwab",
    "csv": "generic", "json": "generic", "auto": "auto",
}


def resolve(name: str | None) -> str:
    if not name:
        return "auto"
    key = name.strip().lower().replace(" ", "")
    key = ALIASES.get(key, key)
    if key not in REGISTRY and key != "auto":
        raise ValueError(f"unknown broker {name!r}; known: {', '.join(sorted(REGISTRY))}")
    return key


def detect(path: str | Path) -> str:
    """Best guess at which broker produced this file."""
    for name in ("ibkr", "schwab"):
        try:
            if REGISTRY[name].detect(path):
                return name
        except Exception:
            continue
    return "generic"


def parse(path: str | Path, *, broker: str | None = None,
          account: str | None = None, **kwargs: Any):
    name = resolve(broker)
    if name == "auto":
        name = detect(path)
    module = REGISTRY[name]
    report = module.parse(path, account=account, **kwargs)
    report["detected_broker"] = name
    return report
