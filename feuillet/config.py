"""Réglages persistants, dans ~/.config/feuillet/config.json."""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "feuillet" / "config.json"

DEFAULTS = {
    "brother_host": "",
    "scanners": [],
    "scanner": "",
    "scan_dir": str(Path.home() / "Documents" / "Scans"),
    "last_printer": "",
    "theme": "sombre",
    "jobs_hidden_before": 0,
}


def migrate_old_data(config_home: Path | None = None, data_home: Path | None = None) -> None:
    """Le projet s'appelait Imprimerie : ses réglages et ses pages en cours suivent le nouveau nom."""
    config_home = config_home or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    data_home = data_home or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    for home in (config_home, data_home):
        old, new = home / "imprimerie", home / "feuillet"
        if old.is_dir() and not new.exists():
            old.rename(new)
    # La session note le chemin complet de ses scans : ils ont changé de dossier.
    session = data_home / "feuillet" / "session" / "session.json"
    try:
        text = session.read_text()
        moved = text.replace(str(data_home / "imprimerie") + "/", str(data_home / "feuillet") + "/")
        if moved != text:
            session.write_text(moved)
    except OSError:
        pass


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        data.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    # Une version précédente notait l'adresse de l'Epson : son scanner reste configuré.
    old_host = data.pop("epson_host", "")
    if old_host and not data["scanners"]:
        device = f"epsonscan2:networkscanner:esci2:network:{old_host}"
        data["scanners"] = [{"id": device, "name": "Epson XP-6000", "backend": "sane", "device": device, "host": old_host}]
        data["scanner"] = device
    return data


def save(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(CONFIG_PATH)
