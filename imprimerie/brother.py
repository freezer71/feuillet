"""Pilote de la Brother QL-1060N : génération du raster et envoi direct (port 9100).

Le format suit le protocole raster Brother QL, tel que décrit par le code source de
brother_ql. tests/test_core.py en vérifie la structure octet par octet.
"""
from __future__ import annotations

import re
import socket
import struct
from dataclasses import dataclass

from PIL import Image, ImageOps

ROW_BYTES = 162          # 1296 points par ligne sur la QL-1050/1060N
ROW_DOTS = ROW_BYTES * 8
MODEL_OFFSET_R = 44      # décalage propre au modèle, en points
MAX_LENGTH_DOTS = 35433
MIN_LENGTH_DOTS = 295
PORT = 9100


@dataclass(frozen=True)
class Label:
    id: str
    name: str
    tape: tuple[int, int]      # largeur, longueur en mm ; longueur 0 = continu
    dots: tuple[int, int]      # zone imprimable en points à 300 dpi ; hauteur 0 = continu
    offset_r: int
    feed: int = 0

    @property
    def endless(self) -> bool:
        return self.tape[1] == 0


LABELS = [
    Label("102x152", "102 × 152 mm (colis)", (102, 153), (1164, 1660), 12),
    Label("102x51", "102 × 51 mm", (102, 51), (1164, 526), 12),
    Label("102", "102 mm continu", (102, 0), (1164, 0), 12, 35),
    Label("62x100", "62 × 100 mm", (62, 100), (696, 1109), 12),
    Label("62x29", "62 × 29 mm", (62, 29), (696, 271), 12),
    Label("62", "62 mm continu", (62, 0), (696, 0), 12, 35),
    Label("54", "54 mm continu", (54, 0), (590, 0), 0, 35),
    Label("50", "50 mm continu", (50, 0), (554, 0), 12, 35),
    Label("38", "38 mm continu", (38, 0), (413, 0), 12, 35),
    Label("29x90", "29 × 90 mm", (29, 90), (306, 991), 6),
    Label("29x42", "29 × 42 mm", (29, 42), (306, 425), 6),
    Label("29", "29 mm continu", (29, 0), (306, 0), 6, 35),
    Label("17x54", "17 × 54 mm", (17, 54), (165, 566), 0),
    Label("12", "12 mm continu", (12, 0), (106, 0), 29, 35),
]
LABELS_BY_ID = {label.id: label for label in LABELS}


def label_from_media(text: str | None) -> Label | None:
    """Trouve l'étiquette correspondant au texte SNMP, ex. '102mm x 152mm / 4" x 6"'."""
    if not text:
        return None
    match = re.search(r"(\d+)\s*mm\s*x\s*(\d+)\s*mm", text, re.I)
    if match:
        return LABELS_BY_ID.get(f"{match[1]}x{match[2]}")
    match = re.search(r"(\d+)\s*mm", text, re.I)
    return LABELS_BY_ID.get(match[1]) if match else None


def _flatten(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("L")
    return image.convert("L")


def fit_to_label(image: Image.Image, label: Label) -> Image.Image:
    """Tourne si besoin et met à l'échelle sans déformer, centré sur l'étiquette."""
    image = _flatten(image)
    width, height = label.dots
    if label.endless:
        new_height = max(1, round(image.height * width / image.width))
        return image.resize((width, new_height), Image.Resampling.LANCZOS)
    if (image.width > image.height) != (width > height):
        image = image.rotate(90, expand=True)
    scale = min(width / image.width, height / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    resized = image.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("L", (width, height), 255)
    canvas.paste(resized, ((width - size[0]) // 2, (height - size[1]) // 2))
    return canvas


def to_bitmap(image: Image.Image, threshold: int = 70, dither: bool = False) -> Image.Image:
    """Passe en noir et blanc. Un seuil élevé donne une impression plus foncée."""
    gray = image.convert("L")
    if dither:
        return gray.convert("1")
    cut = int((100 - threshold) / 100 * 255)
    return gray.point(lambda value: 255 if 255 - value < cut else 0, mode="1")


def raster_frame(bitmap: Image.Image, label: Label) -> bytes:
    """Traduit un bitmap déjà à la taille de l'étiquette en commandes d'une page."""
    width = label.dots[0]
    if bitmap.width != width:
        raise ValueError(f"Largeur {bitmap.width}, attendu {width}")
    if not label.endless and bitmap.height != label.dots[1]:
        raise ValueError(f"Hauteur {bitmap.height}, attendu {label.dots[1]}")
    if label.endless and not MIN_LENGTH_DOTS <= bitmap.height <= MAX_LENGTH_DOTS:
        raise ValueError("Longueur d'étiquette hors limites pour la QL-1060N")

    canvas = Image.new("1", (ROW_DOTS, bitmap.height), 1)
    canvas.paste(bitmap.convert("1"), (ROW_DOTS - width - MODEL_OFFSET_R - label.offset_r, 0))
    # Un bit à 1 signifie « imprimer » : on inverse, et l'imprimante reçoit l'image en miroir.
    raw = ImageOps.mirror(ImageOps.invert(canvas.convert("L"))).convert("1").tobytes()

    media = 0x0A if label.endless else 0x0B
    flags = 0x80 | 0x02 | 0x04 | 0x08 | 0x40   # type, largeur, longueur, qualité
    out = bytearray()
    out += b"\x1b\x69\x53"                     # demande d'état
    out += b"\x1b\x69\x7a" + bytes([flags, media, label.tape[0], label.tape[1]])
    out += struct.pack("<L", bitmap.height) + b"\x00\x00"
    out += b"\x1b\x69\x4d\x40"                 # coupe automatique
    out += b"\x1b\x69\x41\x01"                 # à chaque étiquette
    out += b"\x1b\x69\x4b\x08"                 # coupe à la fin
    out += b"\x1b\x69\x64" + struct.pack("<H", label.feed)
    for start in range(0, len(raw), ROW_BYTES):
        out += b"\x67\x00" + bytes([ROW_BYTES]) + raw[start:start + ROW_BYTES]
    return bytes(out)


def build_job(bitmaps: list[Image.Image], label: Label) -> bytes:
    """Assemble un travail complet, prêt à envoyer."""
    if not bitmaps:
        raise ValueError("Rien à imprimer")
    out = bytearray(b"\x1b\x69\x61\x01" + b"\x00" * 200 + b"\x1b\x40" + b"\x1b\x69\x61\x01")
    for index, bitmap in enumerate(bitmaps):
        out += raster_frame(bitmap, label)
        out += b"\x1a" if index == len(bitmaps) - 1 else b"\x0c"
    return bytes(out)


def send(data: bytes, host: str, timeout: float = 10.0) -> None:
    with socket.create_connection((host, PORT), timeout=timeout) as connection:
        connection.settimeout(60.0)
        connection.sendall(data)
