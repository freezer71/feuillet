"""Ouvre n'importe quel document et le rend en images, pour l'aperçu et les étiquettes."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml", ".py", ".sh", ".ini", ".conf"}
HTML_EXT = {".html", ".htm"}
OFFICE_EXT = {".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp"}
OPEN_FILTER = ("Documents (*.pdf " + " ".join(f"*{e}" for e in sorted(IMAGE_EXT | TEXT_EXT | HTML_EXT | OFFICE_EXT))
               + ");;Tous les fichiers (*)")


class DocumentError(RuntimeError):
    pass


def _workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="feuillet-"))


def _text_to_pdf(source: Path, html: bool) -> Path:
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPdfWriter, QTextDocument

    target = _workdir() / (source.stem + ".pdf")
    writer = QPdfWriter(str(target))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(15, 15, 15, 15), QPageLayout.Unit.Millimeter)
    writer.setResolution(300)
    document = QTextDocument()
    content = source.read_text(errors="replace")
    if html:
        document.setHtml(content)
    else:
        document.setDefaultFont(QFont("monospace", 10))
        document.setPlainText(content)
    document.print_(writer)
    return target


def _office_to_pdf(source: Path) -> Path:
    office = shutil.which("soffice") or shutil.which("libreoffice")
    if not office:
        raise DocumentError("Pour ouvrir ce format, installe LibreOffice, ou exporte le fichier en PDF.")
    out = _workdir()
    result = subprocess.run([office, "--headless", "--convert-to", "pdf", "--outdir", str(out), str(source)],
                            capture_output=True, text=True, timeout=180)
    target = out / (source.stem + ".pdf")
    if result.returncode != 0 or not target.exists():
        raise DocumentError(f"LibreOffice n'a pas pu convertir ce fichier : {result.stderr.strip()[:200]}")
    return target


class Document:
    def __init__(self, path: str | Path):
        self.source = Path(path)
        if not self.source.is_file():
            raise DocumentError(f"Fichier introuvable : {self.source}")
        self.name = self.source.name
        ext = self.source.suffix.lower()
        self.is_image = ext in IMAGE_EXT
        if self.is_image:
            try:
                with Image.open(self.source) as image:
                    self.page_count = getattr(image, "n_frames", 1)
            except OSError as error:
                raise DocumentError(f"Image illisible : {error}") from error
            self.printable = self.source
            return
        if ext == ".pdf":
            pdf = self.source
        elif ext in OFFICE_EXT:
            pdf = _office_to_pdf(self.source)
        elif ext in HTML_EXT or ext in TEXT_EXT:
            pdf = _text_to_pdf(self.source, ext in HTML_EXT)
        elif self.source.read_bytes()[:5] == b"%PDF-":
            pdf = self.source
        else:
            try:
                self.source.read_text()
            except UnicodeDecodeError as error:
                raise DocumentError(f"Format non pris en charge : {ext or self.name}") from error
            pdf = _text_to_pdf(self.source, False)
        self.printable = pdf
        self.page_count = _pdf_pages(pdf)

    def render(self, page: int, dpi: int = 150) -> Image.Image:
        if self.is_image:
            with Image.open(self.source) as image:
                image.seek(page)
                keep_alpha = image.mode in ("RGBA", "LA", "P")
                return ImageOps.exif_transpose(image).convert("RGBA" if keep_alpha else "RGB")
        with tempfile.TemporaryDirectory(prefix="feuillet-") as tmp:
            base = Path(tmp) / "page"
            result = subprocess.run(
                ["pdftocairo", "-png", "-singlefile", "-cropbox", "-r", str(dpi), "-f", str(page + 1), "-l", str(page + 1),
                 str(self.printable), str(base)], capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                raise DocumentError(f"Page illisible : {result.stderr.strip()[:200]}")
            with Image.open(base.with_suffix(".png")) as image:
                return image.convert("RGB")


def _pdf_pages(path: Path) -> int:
    result = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=30)
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    message = result.stderr.strip()
    if "password" in message.lower():
        raise DocumentError("Ce PDF est protégé par un mot de passe.")
    raise DocumentError(f"PDF illisible : {message[:200]}")


def crop_normalized(image: Image.Image, rect: tuple[float, float, float, float]) -> Image.Image:
    """Découpe selon une zone en proportions (x, y, largeur, hauteur)."""
    x, y, w, h = rect
    box = (round(x * image.width), round(y * image.height),
           round((x + w) * image.width), round((y + h) * image.height))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError("Zone de découpe vide")
    return image.crop(box)


def tesseract_langs() -> list[str]:
    try:
        result = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10)
    except OSError:
        return []
    return [line.strip() for line in result.stdout.splitlines()[1:] if line.strip() and line.strip() != "osd"]


def pages_to_pdf(images: list[Image.Image], target: Path, dpi: int, jpeg_quality: int = 90) -> None:
    if all(image.mode == "1" for image in images):
        # Le noir et blanc pur est compressé sans perte : le réglage de qualité JPEG lui est refusé.
        images[0].save(target, "PDF", save_all=True, append_images=images[1:], resolution=float(dpi))
        return
    converted = [image.convert("L") if image.mode == "1" else image if image.mode == "L" else image.convert("RGB")
                 for image in images]
    converted[0].save(target, "PDF", save_all=True, append_images=converted[1:], resolution=float(dpi),
                      quality=jpeg_quality)


def image_to_ocr_pdf(image: Image.Image, target: Path, dpi: int, lang: str) -> None:
    """Une page PDF avec une couche de texte cherchable."""
    with tempfile.TemporaryDirectory(prefix="feuillet-") as tmp:
        source = Path(tmp) / "page.png"
        image.save(source, dpi=(dpi, dpi))
        base = Path(tmp) / "page"
        result = subprocess.run(["tesseract", str(source), str(base), "-l", lang, "--dpi", str(dpi), "pdf"],
                                capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise DocumentError(f"Reconnaissance du texte impossible : {result.stderr.strip()[:200]}")
        shutil.copyfile(base.with_suffix(".pdf"), target)
