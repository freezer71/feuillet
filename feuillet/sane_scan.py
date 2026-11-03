"""Scan générique : le pilote SANE d'un appareil, ou eSCL pour un scanner réseau sans pilote."""
from __future__ import annotations

import io
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from . import escl

AREA_MM = {"A4": (210, 297), "A5": (148, 210), "A6": (105, 148), "Lettre US": (215.9, 279.4)}
FULL_AREA = "Vitre entière"
AREA_NAMES = {"A4": "A4", "A5": "A5", "A6": "A6", "Lettre US": "Letter", FULL_AREA: "Maximum"}
COLOR, GRAY, BLACK_WHITE = "Couleur", "Niveaux de gris", "Noir et blanc"
COMMON_RESOLUTIONS = (100, 150, 200, 300, 600, 1200)
# Durée maximale selon la résolution : au-delà, le scan est considéré comme bloqué.
TIMEOUTS = {100: 90, 150: 100, 200: 120, 300: 180, 600: 420, 1200: 900}


class ScanError(RuntimeError):
    pass


@dataclass
class Capabilities:
    resolutions: list[int] = field(default_factory=lambda: [100, 200, 300, 600])
    colors: list[str] = field(default_factory=lambda: [COLOR, GRAY, BLACK_WHITE])
    areas: list[str] = field(default_factory=lambda: ["A4", FULL_AREA, "A5", "A6", "Lettre US"])
    modes: dict = field(default_factory=dict)       # libellé : valeur du pilote
    sources: list[str] = field(default_factory=list)
    named_areas: bool = False
    max_mm: tuple[float, float] | None = None


def parse_options(text: str) -> Capabilities:
    """Lit la sortie de scanimage -A, dont le format dépend du pilote."""
    caps = Capabilities()
    match = re.search(r"--mode\s+(\S.*?)\s+\[", text)
    if match:
        for value in match[1].split("|"):
            lowered = value.lower()
            if "color" in lowered or "colour" in lowered:
                caps.modes.setdefault(COLOR, value)
            elif "gray" in lowered or "grey" in lowered:
                caps.modes.setdefault(GRAY, value)
    caps.colors = [name for name in (COLOR, GRAY) if name in caps.modes]
    if GRAY in caps.modes:
        caps.colors.append(BLACK_WHITE)      # calculé à partir du gris, pour un résultat net
    match = re.search(r"--resolution\s+(\S+?)\s*dpi", text)
    if match:
        if ".." in match[1]:
            low, high = (float(value) for value in match[1].split(".."))
            caps.resolutions = [value for value in COMMON_RESOLUTIONS if low <= value <= high]
        else:
            caps.resolutions = sorted({int(float(value)) for value in match[1].split("|") if value})
    match = re.search(r"--source\s+(\S.*?)\s+\[", text)
    if match:
        caps.sources = list(dict.fromkeys(match[1].split("|")))
    caps.named_areas = bool(re.search(r"--scan-area\s+\S*A4", text))
    width, height = re.search(r"-x\s+[0-9.]+\.\.([0-9.]+)mm", text), re.search(r"-y\s+[0-9.]+\.\.([0-9.]+)mm", text)
    if width and height:
        caps.max_mm = (float(width[1]), float(height[1]))
    caps.areas = [FULL_AREA]
    for name, (w, h) in AREA_MM.items():
        if caps.named_areas or caps.max_mm is None or (w <= caps.max_mm[0] + 1 and h <= caps.max_mm[1] + 1):
            caps.areas.append(name)
    if "A4" in caps.areas:
        caps.areas.remove("A4")
        caps.areas.insert(0, "A4")
    return caps


def scan_args(device: str, caps: Capabilities, resolution: int, color: str, area: str, output: str, source: str = "") -> list[str]:
    native = caps.modes.get(GRAY if color == BLACK_WHITE else color)
    args = ["scanimage", "-d", device, "--resolution", str(resolution), "--format=png", "-o", output]
    if native:
        args += ["--mode", native]
    if source:
        args += ["--source", source]
    if caps.named_areas:
        args += ["--scan-area", AREA_NAMES[area]]
    elif area != FULL_AREA:
        width, height = AREA_MM[area]
        args += ["-l", "0", "-t", "0", "-x", f"{width:g}", "-y", f"{height:g}"]
    return args


def explain(stderr: str) -> str:
    text = stderr.strip()
    lowered = text.lower()
    if "out of documents" in lowered or "busy" in lowered:
        return ("Le scanner n'est pas disponible. Regarde si un message s'affiche sur l'écran "
                "de l'imprimante, ferme-le, puis réessaie.")
    if "invalid argument" in lowered or "no such" in lowered or "open" in lowered:
        return f"Scanner introuvable. Vérifie les Réglages. ({text[:120]})"
    return f"Le scan a échoué : {text[:200] or 'erreur inconnue'}"


def capabilities(scanner: dict) -> Capabilities:
    if scanner["backend"] == "escl":
        found = escl.Scanner(scanner["host"]).capabilities()
        caps = Capabilities(resolutions=found.resolutions)
        return caps
    try:
        result = subprocess.run(["scanimage", "-d", scanner["device"], "-A"], capture_output=True, text=True, timeout=45)
    except FileNotFoundError as error:
        raise ScanError("scanimage n'est pas installé (paquet sane).") from error
    except subprocess.TimeoutExpired as error:
        raise ScanError("Le scanner ne répond pas.") from error
    if result.returncode != 0:
        raise ScanError(explain(result.stderr))
    return parse_options(result.stdout)


def _scan_sane(scanner: dict, caps: Capabilities, resolution: int, color: str, area: str, source: str) -> Image.Image:
    with tempfile.TemporaryDirectory(prefix="feuillet-") as tmp:
        target = Path(tmp) / "scan.png"
        try:
            result = subprocess.run(scan_args(scanner["device"], caps, resolution, color, area, str(target), source),
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


def scan(scanner: dict, caps: Capabilities, resolution: int, color: str, area: str, source: str = "") -> Image.Image:
    """Lance un scan et renvoie l'image."""
    if scanner["backend"] == "escl":
        client = escl.Scanner(scanner["host"])
        hundredths = None
        if area != FULL_AREA:
            width, height = AREA_MM[area]
            hundredths = (round(width / 25.4 * 300), round(height / 25.4 * 300))
        try:
            data = client.scan(resolution, "RGB24" if color == COLOR else "Grayscale8", hundredths)
        except escl.ScanError as error:
            raise ScanError(str(error)) from error
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return image.copy()
    return _scan_sane(scanner, caps, resolution, color, area, source)
