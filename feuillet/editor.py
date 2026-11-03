"""Modèle de l'éditeur : une liste de pages (scans, images, pages de PDF) et leur export.

Les retouches ne modifient jamais les fichiers d'origine : une page garde sa source, sa
rotation, sa zone de recadrage et son format de sortie, appliqués seulement à l'export.
Les pages de PDF restent vectorielles quand aucune reconnaissance de texte n'est demandée.
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageOps

from . import documents

try:
    import pypdf
except ImportError:
    pypdf = None

# Une numérisation à 1200 dpi dépasse le seuil d'alerte de Pillow : ce sont nos propres fichiers.
Image.MAX_IMAGE_PIXELS = 400_000_000

PYPDF_HINT = "L'export PDF demande pypdf : sudo pacman -S python-pypdf"
MM_PER_INCH = 25.4
PT_PER_MM = 72 / MM_PER_INCH

# Largeur et hauteur en mm ; None laisse la zone recadrée à sa taille réelle.
FORMATS: dict[str, tuple[float, float] | None] = {
    "Libre": None,
    "A4 portrait": (210, 297),
    "A4 paysage": (297, 210),
    "A5 portrait": (148, 210),
    "A5 paysage": (210, 148),
    "A6 portrait": (105, 148),
    "A6 paysage": (148, 105),
    "Lettre US portrait": (215.9, 279.4),
    "Lettre US paysage": (279.4, 215.9),
}

Rect = tuple[float, float, float, float]   # x, y, largeur, hauteur, en proportions de la page
_ids = itertools.count(1)


def new_uid() -> int:
    return next(_ids)


@dataclass(frozen=True)
class Page:
    source: Path
    index: int = 0
    is_pdf: bool = False
    dpi: int = 0                            # résolution d'un scan ; 0 si inconnue
    name: str = ""
    rotation: int = 0                       # 0, 90, 180 ou 270 degrés, sens horaire
    crop: Rect | None = None
    target: tuple[float, float] | None = None
    uid: int = 0
    angle: float = 0.0                      # redressement fin, en degrés, sens horaire
    filter: str = "original"
    brightness: int = 0                     # -100 à 100
    contrast: int = 0                       # -100 à 100


_documents: dict[Path, documents.Document] = {}


def _document(path: Path) -> documents.Document:
    if path not in _documents:
        _documents[path] = documents.Document(path)
    return _documents[path]


def pages_from_file(path: str | Path, keep_in: Path | None = None) -> list[Page]:
    """Transforme un fichier en pages : chaque page d'un PDF devient une page de l'éditeur.

    Un texte ou un fichier Office est converti dans un dossier temporaire : keep_in en garde
    une copie, pour que la session reste valable au prochain démarrage.
    """
    document = documents.Document(path)
    source = Path(document.printable)
    if keep_in and source != Path(path):
        kept = keep_in / f"import-{new_uid()}-{source.name}"
        shutil.copyfile(source, kept)
        document.printable = kept
        source = kept
    _documents[source] = document
    return [Page(source=source, index=index, is_pdf=not document.is_image, name=document.name, uid=new_uid())
            for index in range(document.page_count)]


def page_from_scan(image: Image.Image, dpi: int, folder: Path, number: int) -> Page:
    """Enregistre le scan sur le disque, pour ne pas garder toutes les pages en mémoire.

    Le PNG est peu compressé : à 600 dpi, l'écriture reste de l'ordre de quelques secondes.
    """
    path = folder / f"scan-{new_uid()}.png"
    image.save(path, dpi=(dpi, dpi), compress_level=1)
    return Page(source=path, dpi=dpi, name=f"Scan {number}", uid=new_uid())


def blank_page(folder: Path, number: int) -> Page:
    page = page_from_scan(Image.new("L", (1240, 1754), 255), 150, folder, number)
    return replace(page, name=f"Page vierge {number}")


def rotate_rect(rect: Rect, degrees: int) -> Rect:
    """Fait tourner une zone avec la page, par quarts de tour horaires."""
    x, y, w, h = rect
    for _ in range((degrees // 90) % 4):
        x, y, w, h = 1 - y - h, x, h, w
    return x, y, w, h


def rotated(page: Page, degrees: int) -> Page:
    turned = (degrees // 90) % 4
    target = page.target
    if target and turned % 2:
        target = (target[1], target[0])
    crop = rotate_rect(page.crop, degrees) if page.crop else None
    return replace(page, rotation=(page.rotation + degrees) % 360, crop=crop, target=target)


def largest_crop(page_ratio: float, target_ratio: float) -> Rect:
    """Plus grande zone centrée au format voulu. Les rapports sont largeur / hauteur."""
    width = target_ratio / page_ratio
    if width <= 1:
        return (1 - width) / 2, 0.0, width, 1.0
    height = 1 / width
    return 0.0, (1 - height) / 2, 1.0, height


def with_format(page: Page, target: tuple[float, float] | None, page_ratio: float) -> Page:
    if target is None:
        return replace(page, target=None)
    return replace(page, target=target, crop=largest_crop(page_ratio, target[0] / target[1]))


def apply_rotation(image: Image.Image, rotation: int) -> Image.Image:
    transforms = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    return image.transpose(transforms[rotation]) if rotation in transforms else image


def source_dpi(page: Page, size: tuple[int, int], info_dpi=None) -> float:
    """Résolution d'une image. Sans information, le côté long est ramené à celui d'un A4."""
    if page.dpi:
        return float(page.dpi)
    if info_dpi and info_dpi[0] and info_dpi[0] > 1:
        return float(info_dpi[0])
    return max(max(size) / (297 / MM_PER_INCH), 1.0)


def render_base(page: Page, dpi: int = 150) -> tuple[Image.Image, float]:
    """Rend la page d'origine, sans retouche. Renvoie l'image et sa résolution effective."""
    document = _document(page.source)
    if document.is_image:
        with Image.open(page.source) as original:
            info = original.info.get("dpi")
        image = document.render(page.index)
        return image, source_dpi(page, image.size, info)
    return document.render(page.index, dpi), float(dpi)


FILTERS = {"original": "Original", "document": "Document net", "gris": "Niveaux de gris", "nb": "Noir et blanc"}


def adjusted(page: Page) -> bool:
    """Vrai si la page a des retouches d'image : une page de PDF passe alors en image à l'export."""
    return bool(page.angle or page.brightness or page.contrast or page.filter != "original")


def otsu_threshold(gray: Image.Image) -> int:
    histogram = np.array(gray.histogram(), dtype=np.float64)
    total = histogram.sum()
    if total == 0:
        return 128
    levels = np.arange(256)
    weight = np.cumsum(histogram)
    cumulative = np.cumsum(histogram * levels)
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (cumulative[-1] * weight / total - cumulative) ** 2 / (weight * (total - weight))
    return int(np.nanargmax(between))


def apply_filter(image: Image.Image, name: str, brightness: int = 0, contrast: int = 0) -> Image.Image:
    if image.mode not in ("RGB", "L"):
        image = _on_white(image).convert("RGB")
    if brightness:
        image = ImageEnhance.Brightness(image).enhance(1 + brightness / 100)
    if contrast:
        image = ImageEnhance.Contrast(image).enhance(1 + contrast / 100)
    if name == "gris":
        return image.convert("L")
    if name == "document":
        # Le fond du papier passe au blanc, l'encre reste nette.
        gray = ImageOps.autocontrast(image.convert("L"), cutoff=(1, 5))
        return gray.point(lambda value: min(255, round(255 * (value / 255) ** 0.8)))
    if name == "nb":
        gray = image.convert("L")
        limit = otsu_threshold(gray)
        return gray.point(lambda value: 255 if value > limit else 0, mode="1")
    return image


def _on_white(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        return Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba).convert("RGB")
    return image


def apply_view(image: Image.Image, page: Page) -> Image.Image:
    """Toutes les retouches sauf le recadrage : ce que montre le plan de travail."""
    image = apply_rotation(_on_white(image), page.rotation)
    if page.angle:
        image = image.convert("RGB").rotate(-page.angle, Image.Resampling.BICUBIC, fillcolor="white")
    if page.filter != "original" or page.brightness or page.contrast:
        image = apply_filter(image, page.filter, page.brightness, page.contrast)
    return image


def render_final(page: Page, dpi: int = 300) -> tuple[Image.Image, float]:
    """Rend la page telle qu'elle sera exportée (retouches et recadrage inclus)."""
    image, effective = render_base(page, dpi)
    image = apply_view(image, page)
    if page.crop:
        image = documents.crop_normalized(image, page.crop)
    return image, effective


_detail_lock = threading.Lock()
_detail: tuple[tuple, Image.Image] | None = None


def render_detail(page: Page, rect: Rect, size: tuple[int, int]) -> Image.Image:
    """Une zone de la page en pleine définition, pour un zoom net. Garde la dernière page en mémoire."""
    global _detail
    key = (page.source, page.index, page.rotation, page.angle, page.filter, page.brightness, page.contrast)
    with _detail_lock:
        if _detail is None or _detail[0] != key:
            base, _dpi = render_base(page, 300)
            _detail = (key, apply_view(base, page))
        image = _detail[1]
    region = documents.crop_normalized(image, rect)
    if region.mode == "1":
        region = region.convert("L")
    return region.resize((max(1, size[0]), max(1, size[1])),
                         Image.Resampling.LANCZOS if region.width > size[0] else Image.Resampling.NEAREST)


def forget_detail() -> None:
    global _detail
    with _detail_lock:
        _detail = None


def detect_content(image: Image.Image, margin: float = 0.01) -> Rect | None:
    """Repère le contenu par rapport à la couleur du fond. None si la page paraît vide."""
    small = image.convert("L")
    small.thumbnail((800, 800))
    pixels = np.asarray(small, dtype=np.int16)
    edge = np.concatenate((pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]))
    mask = np.abs(pixels - np.median(edge)) > 30
    rows = np.flatnonzero(mask.mean(axis=1) > 0.004)
    columns = np.flatnonzero(mask.mean(axis=0) > 0.004)
    if rows.size == 0 or columns.size == 0:
        return None
    height, width = pixels.shape
    x0, x1 = max(0.0, columns[0] / width - margin), min(1.0, (columns[-1] + 1) / width + margin)
    y0, y1 = max(0.0, rows[0] / height - margin), min(1.0, (rows[-1] + 1) / height + margin)
    return x0, y0, x1 - x0, y1 - y0


def detect_skew(image: Image.Image, limit: float = 6.0, step: float = 0.25) -> float:
    """Trouve l'angle à appliquer : les lignes de texte droites donnent les profils les plus marqués."""
    small = image.convert("L")
    small.thumbnail((700, 700))
    limit_value = otsu_threshold(small)
    ink = small.point(lambda value: 255 if value <= limit_value else 0)
    best, best_score = 0.0, -1.0
    for tenth in np.arange(-limit, limit + step / 2, step):
        rotated_ink = ink.rotate(float(tenth), Image.Resampling.NEAREST)
        rows = np.asarray(rotated_ink, dtype=np.float64).sum(axis=1)
        score = float(np.square(np.diff(rows)).sum())
        if score > best_score:
            best, best_score = float(tenth), score
    return -best


# --- Texte sélectionnable ---------------------------------------------------------------
# Un mot : (texte, x, y, largeur, hauteur, numéro de ligne), en proportions de la page affichée.
_words: dict[tuple, list[tuple]] = {}


def parse_bbox(xml: str) -> list[tuple]:
    """Lit la sortie de pdftotext -bbox."""
    root = ET.fromstring(xml)
    words: list[tuple] = []
    line, last_middle = 0, None
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "page":
            width, height = float(node.get("width")), float(node.get("height"))
        elif tag == "word" and (node.text or "").strip():
            x0, y0, x1, y1 = (float(node.get(key)) for key in ("xMin", "yMin", "xMax", "yMax"))
            middle = (y0 + y1) / 2
            if last_middle is not None and abs(middle - last_middle) > (y1 - y0) * 0.6:
                line += 1
            last_middle = middle
            words.append((node.text, x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height, line))
    return words


def parse_tsv(tsv: str, size: tuple[int, int]) -> list[tuple]:
    """Lit la sortie TSV de Tesseract."""
    words: list[tuple] = []
    lines: dict[tuple, int] = {}
    for row in tsv.splitlines()[1:]:
        cells = row.split("	")
        if len(cells) < 12 or cells[0] != "5" or not cells[11].strip():
            continue
        line = lines.setdefault((cells[2], cells[3], cells[4]), len(lines))
        x, y, w, h = (int(value) for value in cells[6:10])
        words.append((cells[11], x / size[0], y / size[1], w / size[0], h / size[1], line))
    return words


def _pdf_words(page: Page) -> list[tuple]:
    number = str(page.index + 1)
    result = subprocess.run(["pdftotext", "-bbox", "-cropbox", "-f", number, "-l", number, str(page.source), "-"],
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    words = parse_bbox(result.stdout)
    turned = []
    for text, x, y, w, h, line in words:
        x, y, w, h = rotate_rect((x, y, w, h), page.rotation)
        turned.append((text, x, y, w, h, line))
    return turned


def page_words(page: Page, lang: str) -> tuple[list[tuple], bool]:
    """Les mots de la page, et s'ils viennent d'une reconnaissance optique."""
    key = (page.source, page.index, page.rotation, page.angle)
    if key in _words:
        return _words[key], False
    if page.is_pdf and not page.angle:
        words = _pdf_words(page)
        if words:
            _words[key] = words
            return words, False
    if not lang:
        raise RuntimeError("Aucune langue Tesseract n'est installée pour lire le texte.")
    base, dpi = render_base(page, 300)
    image = apply_view(base, replace(page, filter="original", brightness=0, contrast=0))
    if dpi > 400:                   # au-delà, la lecture est bien plus lente sans gagner en précision
        scale = 300 / dpi
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
        dpi = 300
    with tempfile.TemporaryDirectory(prefix="feuillet-") as tmp:
        source = Path(tmp) / "page.png"
        image.save(source, dpi=(dpi, dpi), compress_level=1)
        result = subprocess.run(["tesseract", str(source), "stdout", "-l", lang, "--dpi", str(round(dpi)), "tsv"],
                                capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"Lecture du texte impossible : {result.stderr.strip()[:200]}")
    words = parse_tsv(result.stdout, image.size)
    _words[key] = words
    return words, True


# --- Session ----------------------------------------------------------------------------

def data_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "feuillet" / "session"


def save_session(pages: list[Page], folder: Path, scan_count: int) -> None:
    rows = []
    for page in pages:
        row = asdict(page)
        row["source"] = str(page.source)
        rows.append(row)
    temporary = folder / "session.json.tmp"
    temporary.write_text(json.dumps({"scan_count": scan_count, "pages": rows}, ensure_ascii=False))
    temporary.replace(folder / "session.json")


def load_session(folder: Path) -> tuple[list[Page], int, int]:
    """Renvoie les pages retrouvées, le compteur de scans, et le nombre de pages perdues."""
    try:
        data = json.loads((folder / "session.json").read_text())
    except (OSError, ValueError):
        return [], 0, 0
    pages, missing = [], 0
    for row in data.get("pages", []):
        path = Path(row["source"])
        if not path.is_file():
            missing += 1
            continue
        row.update(source=path, uid=new_uid())
        if row.get("crop"):
            row["crop"] = tuple(row["crop"])
        if row.get("target"):
            row["target"] = tuple(row["target"])
        pages.append(Page(**row))
    return pages, int(data.get("scan_count", 0)), missing


def clear_session(folder: Path) -> None:
    """Efface uniquement ce que l'application a créé dans son dossier de session."""
    for path in folder.iterdir():
        if path.is_file() and (path.name.startswith(("scan-", "import-", "document-")) or path.name.startswith("session.json")):
            path.unlink()


def _require_pypdf() -> None:
    if pypdf is None:
        raise RuntimeError(PYPDF_HINT)


def _scale_to_target(pdf_page, target: tuple[float, float] | None) -> None:
    if target:
        pdf_page.scale_to(target[0] * PT_PER_MM, target[1] * PT_PER_MM)


def _set_boxes(pdf_page, box) -> None:
    for name in ("mediabox", "cropbox", "trimbox", "bleedbox", "artbox"):
        setattr(pdf_page, name, box)


def _add_vector_page(writer, reader, page: Page) -> None:
    out = writer.add_page(reader.pages[page.index])
    _set_boxes(out, pypdf.generic.RectangleObject(out.cropbox))
    out.transfer_rotation_to_content()
    if page.rotation:
        out.rotate(page.rotation)
        out.transfer_rotation_to_content()
    if page.crop:
        left, bottom = float(out.mediabox.left), float(out.mediabox.bottom)
        width, height = float(out.mediabox.width), float(out.mediabox.height)
        x, y, w, h = page.crop
        _set_boxes(out, pypdf.generic.RectangleObject(
            (left + x * width, bottom + (1 - y - h) * height, left + (x + w) * width, bottom + (1 - y) * height)))
    _scale_to_target(out, page.target)


def _add_single_page_pdf(writer, path: Path, page: Page) -> None:
    out = writer.add_page(pypdf.PdfReader(path).pages[0])
    _scale_to_target(out, page.target)


QUALITIES = {"Haute": (0, 92), "Standard": (200, 85), "Compacte": (120, 60)}


def _limit_dpi(image: Image.Image, dpi: float, limit: int) -> tuple[Image.Image, float]:
    if limit and dpi > limit:
        scale = limit / dpi
        return image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                            Image.Resampling.LANCZOS), float(limit)
    return image, dpi


def export_pdf(pages: list[Page], target: Path, ocr_lang: str = "", progress=None,
               quality: str = "Haute", password: str = "") -> None:
    """Assemble les pages. Le fichier n'apparaît qu'une fois complet.

    La qualité réduit les pages en image ; les pages de PDF sans retouche restent vectorielles.
    """
    _require_pypdf()
    if not pages:
        raise ValueError("Aucune page à exporter")
    limit, jpeg = QUALITIES[quality]
    writer = pypdf.PdfWriter()
    readers: dict[Path, object] = {}
    with tempfile.TemporaryDirectory(prefix="feuillet-") as tmp:
        for number, page in enumerate(pages):
            single = Path(tmp) / f"{number}.pdf"
            if ocr_lang:
                image, dpi = render_final(page, 300)
                image, dpi = _limit_dpi(image, dpi, limit)
                documents.image_to_ocr_pdf(image, single, round(dpi), ocr_lang)
                _add_single_page_pdf(writer, single, page)
            elif page.is_pdf and not adjusted(page):
                reader = readers.setdefault(page.source, pypdf.PdfReader(page.source))
                _add_vector_page(writer, reader, page)
            else:
                image, dpi = render_final(page)
                image, dpi = _limit_dpi(image, dpi, limit)
                documents.pages_to_pdf([image], single, round(dpi), jpeg)
                _add_single_page_pdf(writer, single, page)
            if progress:
                progress(number + 1, len(pages))
        if password:
            writer.encrypt(password)
        partial = target.with_name(target.name + ".part")
        try:
            with open(partial, "wb") as stream:
                writer.write(stream)
            partial.replace(target)
        except OSError as error:
            partial.unlink(missing_ok=True)
            reason = error.strerror or str(error)
            raise RuntimeError(f"Impossible d'enregistrer dans {target.parent} : {reason}.") from error


def export_image(page: Page, target: Path) -> None:
    image, dpi = render_final(page)
    if target.suffix.lower() in (".jpg", ".jpeg"):
        image.convert("RGB").save(target, dpi=(dpi, dpi), quality=92)
    else:
        image.save(target, dpi=(dpi, dpi))


def pdf_page_sizes_mm(path: Path) -> list[tuple[float, float]]:
    """Tailles réelles des pages, lues avec pdfinfo. Sert à vérifier un export."""
    result = subprocess.run(["pdfinfo", "-f", "1", "-l", "9999", str(path)], capture_output=True, text=True, timeout=30)
    sizes = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 7 and parts[0] == "Page" and parts[2] == "size:":
            sizes.append((float(parts[3]) / PT_PER_MM, float(parts[5]) / PT_PER_MM))
    return sizes
