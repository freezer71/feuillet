"""Réglages persistants, dans ~/.config/imprimerie/config.json."""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "imprimerie" / "config.json"

DEFAULTS = {
    "epson_host": "192.168.0.20",
    "brother_host": "192.168.0.35",
    "scan_dir": str(Path.home() / "Documents" / "Scans"),
    "last_printer": "",
    "theme": "sombre",
    "jobs_hidden_before": 0,
}


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        data.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return data


def save(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(CONFIG_PATH)
