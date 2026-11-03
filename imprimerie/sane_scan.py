"""Scan via le pilote Epson (epsonscan2) en ligne de commande, sans son interface."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image

AREAS = {"A4": "A4", "Vitre entière": "Maximum", "A5": "A5", "A6": "A6", "Lettre US": "Letter"}
COLOR_MODES = {"Couleur": "Color", "Niveaux de gris": "Grayscale", "Noir et blanc": "Grayscale"}
# Durée maximale selon la résolution : au-delà, le scan est considéré comme bloqué.
TIMEOUTS = {100: 90, 200: 120, 300: 180, 600: 420, 1200: 900}


class ScanError(RuntimeError):
    pass


def device_name(host: str) -> str:
    return f"epsonscan2:networkscanner:esci2:network:{host}"


def scan_args(host: str, resolution: int, mode: str, area: str, output: str) -> list[str]:
    return ["scanimage", "-d", device_name(host), "--source", "Flatbed", "--mode", mode,
            "--resolution", str(resolution), "--scan-area", area, "--format=png", "-o", output]


def explain(stderr: str) -> str:
    text = stderr.strip()
    lowered = text.lower()
    if "out of documents" in lowered or "busy" in lowered:
        return ("Le scanner n'est pas disponible. Regarde si un message s'affiche sur l'écran "
                "de l'imprimante, ferme-le, puis réessaie.")
    if "invalid argument" in lowered or "no such" in lowered or "open" in lowered:
        return f"Scanner introuvable à cette adresse. Vérifie la page Appareils. ({text[:120]})"
    return f"Le scan a échoué : {text[:200] or 'erreur inconnue'}"


def scan(host: str, resolution: int, mode: str, area: str) -> Image.Image:
    """Lance un scan à la vitre et renvoie l'image."""
    with tempfile.TemporaryDirectory(prefix="imprimerie-") as tmp:
        target = Path(tmp) / "scan.png"
        try:
            result = subprocess.run(scan_args(host, resolution, mode, area, str(target)),
                                    capture_output=True, text=True, timeout=TIMEOUTS.get(resolution, 420))
        except FileNotFoundError as error:
            raise ScanError("scanimage n'est pas installé (paquet sane).") from error
        except subprocess.TimeoutExpired as error:
            raise ScanError("Le scanner ne répond plus. Regarde si un message s'affiche sur son écran.") from error
        if result.returncode != 0:
            raise ScanError(explain(result.stderr))
        if not target.exists() or target.stat().st_size == 0:
            raise ScanError("Le scanner n'a renvoyé aucune image.")
        with Image.open(target) as image:
            image.load()
            return image.copy()
