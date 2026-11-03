import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from imprimerie import documents, editor
from imprimerie.editor import Page


class GeometryTests(unittest.TestCase):
    def test_rotate_rect_is_a_quarter_turn(self):
        self.assertEqual(editor.rotate_rect((0.0, 0.0, 0.5, 0.25), 90), (0.75, 0.0, 0.25, 0.5))

    def test_four_turns_change_nothing(self):
        rect = (0.1, 0.2, 0.3, 0.4)
        self.assertEqual(tuple(round(v, 6) for v in editor.rotate_rect(rect, 360)), rect)
        result = rect
        for _ in range(4):
            result = editor.rotate_rect(result, 90)
        self.assertEqual(tuple(round(v, 6) for v in result), rect)

    def test_rotation_swaps_the_format(self):
        page = Page(source=Path("x"), crop=(0.0, 0.0, 1.0, 1.0), target=(210, 297))
        turned = editor.rotated(page, 90)
        self.assertEqual(turned.target, (297, 210))
        self.assertEqual(turned.rotation, 90)
        self.assertEqual(editor.rotated(turned, 270).target, (210, 297))

    def test_largest_crop_on_a_wider_page(self):
        x, y, w, h = editor.largest_crop(1.0, 210 / 297)
        self.assertAlmostEqual(w, 210 / 297)
        self.assertEqual(h, 1.0)
        self.assertAlmostEqual(x, (1 - w) / 2)

    def test_largest_crop_on_a_narrower_page(self):
        x, y, w, h = editor.largest_crop(0.5, 1.0)
        self.assertEqual(w, 1.0)
        self.assertAlmostEqual(h, 0.5)
        self.assertAlmostEqual(y, 0.25)

    def test_unknown_image_is_scaled_like_a_a4(self):
        page = Page(source=Path("x"))
        self.assertAlmostEqual(editor.source_dpi(page, (2970, 2000)), 254.0)
        self.assertEqual(editor.source_dpi(Page(source=Path("x"), dpi=300), (10, 10)), 300.0)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, size=(300, 400)):
        image = Image.new("L", size, 255)
        ImageDraw.Draw(image).rectangle((0, 0, 99, 99), fill=0)     # carré noir en haut à gauche
        return editor.page_from_scan(image, 100, self.dir, 1)

    def test_final_render_rotates_then_crops(self):
        page = self.scan()
        turned = editor.rotated(page, 90)     # le carré passe en haut à droite
        image, dpi = editor.render_final(turned)
        image = image.convert("L")
        self.assertEqual(image.size, (400, 300))
        self.assertEqual(image.getpixel((350, 50)), 0)
        self.assertEqual(image.getpixel((50, 50)), 255)
        cropped, _ = editor.render_final(editor.replace(turned, crop=(0.75, 0.0, 0.25, 1 / 3)))
        cropped = cropped.convert("L")
        self.assertEqual(cropped.size, (100, 100))
        self.assertEqual(cropped.getpixel((50, 50)), 0)

    def test_multi_page_file_gives_one_page_each(self):
        path = self.dir / "two.pdf"
        documents.pages_to_pdf([Image.new("RGB", (200, 300), "white")] * 2, path, 100)
        pages = editor.pages_from_file(path)
        self.assertEqual([p.index for p in pages], [0, 1])
        self.assertTrue(all(p.is_pdf for p in pages))
        self.assertEqual(len({p.uid for p in pages}), 2)


class ToolTests(unittest.TestCase):
    def test_filters(self):
        image = Image.new("RGB", (40, 40), (200, 200, 200))
        ImageDraw.Draw(image).rectangle((0, 0, 19, 39), fill=(30, 30, 30))
        self.assertEqual(editor.apply_filter(image, "gris").mode, "L")
        bw = editor.apply_filter(image, "nb")
        self.assertEqual(bw.mode, "1")
        self.assertEqual(bw.getpixel((5, 5)), 0)
        self.assertEqual(bw.getpixel((30, 5)), 255)
        self.assertGreater(editor.apply_filter(image, "original", brightness=50).getpixel((30, 5))[0], 200)

    def test_content_detection(self):
        image = Image.new("L", (800, 1000), 255)
        ImageDraw.Draw(image).rectangle((200, 300, 599, 699), fill=0)
        x, y, w, h = editor.detect_content(image)
        self.assertAlmostEqual(x, 0.25, delta=0.02)
        self.assertAlmostEqual(y, 0.30, delta=0.02)
        self.assertAlmostEqual(w, 0.5, delta=0.04)
        self.assertAlmostEqual(h, 0.4, delta=0.04)
        self.assertIsNone(editor.detect_content(Image.new("L", (100, 100), 255)))

    def test_skew_detection_finds_the_correction(self):
        image = Image.new("L", (700, 700), 255)
        draw = ImageDraw.Draw(image)
        for y in range(60, 660, 28):
            draw.rectangle((80, y, 620, y + 7), fill=0)
        tilted = image.rotate(-3, Image.Resampling.BICUBIC, fillcolor=255)   # penche de 3° en sens horaire
        angle = editor.detect_skew(tilted)
        self.assertAlmostEqual(angle, -3, delta=0.5)
        straightened = editor.apply_view(tilted.convert("RGB"), Page(source=Path("x"), angle=angle))
        self.assertAlmostEqual(editor.detect_skew(straightened), 0, delta=0.5)

    def test_word_parsers(self):
        xml = ('<html xmlns="http://www.w3.org/1999/xhtml"><body><doc><page width="100" height="200">'
               '<word xMin="10" yMin="20" xMax="30" yMax="30">Bon</word>'
               '<word xMin="35" yMin="20" xMax="50" yMax="30">jour</word>'
               '<word xMin="10" yMin="40" xMax="30" yMax="50">Fin</word></page></doc></body></html>')
        words = editor.parse_bbox(xml)
        self.assertEqual([w[0] for w in words], ["Bon", "jour", "Fin"])
        self.assertEqual([w[5] for w in words], [0, 0, 1])
        self.assertAlmostEqual(words[0][1], 0.1)
        header = "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext"
        tsv = header + "\n5\t1\t1\t1\t1\t1\t10\t20\t30\t10\t96\tSalut\n4\t1\t1\t1\t1\t0\t0\t0\t0\t0\t-1\t\n"
        self.assertEqual(editor.parse_tsv(tsv, (100, 100)), [("Salut", 0.1, 0.2, 0.3, 0.1, 0)])

    def test_session_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            page = editor.page_from_scan(Image.new("L", (10, 10), 255), 100, folder, 1)
            page = editor.replace(page, crop=(0.1, 0.1, 0.5, 0.5), target=(210, 297), angle=1.5)
            gone = Page(source=folder / "absent.png", uid=99)
            editor.save_session([page, gone], folder, 7)
            pages, count, missing = editor.load_session(folder)
            self.assertEqual((len(pages), count, missing), (1, 7, 1))
            self.assertEqual(pages[0].crop, (0.1, 0.1, 0.5, 0.5))
            self.assertEqual(pages[0].target, (210, 297))
            mine = folder / "autre.txt"
            mine.write_text("x")
            editor.clear_session(folder)
            self.assertEqual([path.name for path in folder.iterdir()], ["autre.txt"])


@unittest.skipIf(editor.pypdf is None, "pypdf n'est pas installé")
class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def vector_pdf(self, name="doc.pdf", pages=2):
        """Un vrai PDF vectoriel, 200 × 100 pt, avec du texte extractible."""
        from PySide6.QtCore import QSizeF
        from PySide6.QtGui import QGuiApplication, QPageSize, QPainter, QPdfWriter
        QGuiApplication.instance() or QGuiApplication(["test"])
        path = self.dir / name
        writer = QPdfWriter(str(path))
        writer.setPageSize(QPageSize(QSizeF(200, 100), QPageSize.Unit.Point))
        writer.setResolution(72)
        painter = QPainter(writer)
        for number in range(pages):
            if number:
                writer.newPage()
            painter.drawText(10, 40, f"page{number}")
        painter.end()
        return path

    def sizes(self, path):
        return editor.pdf_page_sizes_mm(path)

    def test_merge_and_reorder_keep_order(self):
        doc = editor.pages_from_file(self.vector_pdf())
        scan = editor.page_from_scan(Image.new("L", (413, 583), 255), 50, self.dir, 1)
        target = self.dir / "out.pdf"
        editor.export_pdf([doc[1], scan, doc[0]], target)
        reader = editor.pypdf.PdfReader(target)
        self.assertEqual(len(reader.pages), 3)
        self.assertIn("page1", reader.pages[0].extract_text())
        self.assertIn("page0", reader.pages[2].extract_text())
        self.assertAlmostEqual(self.sizes(target)[1][0], 413 / 50 * 25.4, delta=1)

    def test_vector_pdf_stays_vector_with_crop_and_format(self):
        doc = editor.pages_from_file(self.vector_pdf(pages=1))[0]
        target = self.dir / "a4.pdf"
        editor.export_pdf([editor.replace(doc, crop=(0.0, 0.0, 0.5, 1.0), target=(210, 297))], target)
        self.assertEqual(len(editor.pypdf.PdfReader(target).pages[0].images), 0)
        self.assertIn("page0", editor.pypdf.PdfReader(target).pages[0].extract_text())
        width, height = self.sizes(target)[0]
        self.assertAlmostEqual(width, 210, delta=0.5)
        self.assertAlmostEqual(height, 297, delta=0.5)

    def test_crop_shows_the_right_part_of_a_scan(self):
        image = Image.new("L", (400, 400), 255)
        ImageDraw.Draw(image).rectangle((0, 0, 199, 199), fill=0)
        page = editor.page_from_scan(image, 100, self.dir, 1)
        target = self.dir / "crop.pdf"
        editor.export_pdf([editor.replace(page, crop=(0.0, 0.0, 0.5, 0.5))], target)
        rendered = documents.Document(target).render(0, 100).convert("L")
        self.assertLess(rendered.getpixel((rendered.width // 2, rendered.height // 2)), 40)
        self.assertAlmostEqual(self.sizes(target)[0][0], 2 * 25.4, delta=1)

    def test_vector_crop_and_rotation_match_the_preview(self):
        from PySide6.QtCore import QSizeF
        from PySide6.QtGui import QColor, QGuiApplication, QPageSize, QPainter, QPdfWriter
        QGuiApplication.instance() or QGuiApplication(["test"])
        path = self.dir / "square.pdf"
        writer = QPdfWriter(str(path))
        writer.setPageSize(QPageSize(QSizeF(200, 100), QPageSize.Unit.Point))
        writer.setResolution(72)
        painter = QPainter(writer)
        painter.fillRect(0, 0, 50, 50, QColor("black"))     # coin haut gauche, à l'affichage
        painter.end()
        page = editor.pages_from_file(path)[0]
        turned = editor.rotated(page, 90)
        expected, _ = editor.render_final(editor.replace(turned, crop=(0.5, 0.0, 0.5, 0.25)), 72)
        target = self.dir / "turned.pdf"
        editor.export_pdf([editor.replace(turned, crop=(0.5, 0.0, 0.5, 0.25))], target)
        rendered = documents.Document(target).render(0, 72).convert("L")
        self.assertEqual(rendered.size, expected.size)
        self.assertLess(rendered.getpixel((rendered.width // 2, rendered.height // 2)), 40)
        self.assertLess(expected.convert("L").getpixel((expected.width // 2, expected.height // 2)), 40)

    def _marked_pdf(self, name, transform=None):
        """Page de 200 × 100 pt avec un carré noir en haut à gauche, éventuellement transformée."""
        from PySide6.QtCore import QSizeF
        from PySide6.QtGui import QColor, QGuiApplication, QPageSize, QPainter, QPdfWriter
        QGuiApplication.instance() or QGuiApplication(["test"])
        raw = self.dir / f"raw-{name}"
        writer = QPdfWriter(str(raw))
        writer.setPageSize(QPageSize(QSizeF(200, 100), QPageSize.Unit.Point))
        writer.setResolution(72)
        painter = QPainter(writer)
        painter.fillRect(0, 0, 50, 50, QColor("black"))
        painter.end()
        if transform is None:
            return raw
        source = editor.pypdf.PdfWriter()
        page = source.add_page(editor.pypdf.PdfReader(raw).pages[0])
        transform(page)
        path = self.dir / name
        with open(path, "wb") as stream:
            source.write(stream)
        return path

    def assert_export_matches_preview(self, page, dpi=72):
        """Ce qui est exporté doit être, au pixel près ou presque, ce que montre l'éditeur."""
        import numpy as np
        expected = editor.render_final(page, dpi)[0].convert("L")
        target = self.dir / f"out-{page.uid}-{page.rotation}.pdf"
        editor.export_pdf([page], target)
        rendered = documents.Document(target).render(0, dpi).convert("L")
        self.assertLessEqual(abs(rendered.width - expected.width), 1)
        self.assertLessEqual(abs(rendered.height - expected.height), 1)
        resized = rendered.resize(expected.size)
        difference = np.abs(np.asarray(resized, dtype=np.int16) - np.asarray(expected, dtype=np.int16))
        self.assertLess(float((difference > 80).mean()), 0.02)

    def test_all_rotations_match_preview(self):
        page = editor.pages_from_file(self._marked_pdf("m.pdf"))[0]
        for degrees in (0, 90, 180, 270):
            self.assert_export_matches_preview(editor.rotated(page, degrees))

    def test_crop_and_rotation_match_preview(self):
        page = editor.pages_from_file(self._marked_pdf("m.pdf"))[0]
        for degrees in (0, 90, 180, 270):
            turned = editor.rotated(page, degrees)
            self.assert_export_matches_preview(editor.replace(turned, crop=(0.1, 0.05, 0.6, 0.7)))

    def test_pdf_already_rotated_matches_preview(self):
        path = self._marked_pdf("r.pdf", lambda page: page.rotate(90))
        page = editor.pages_from_file(path)[0]
        self.assert_export_matches_preview(page)
        self.assert_export_matches_preview(editor.rotated(page, 90))
        self.assert_export_matches_preview(editor.replace(editor.rotated(page, 270), crop=(0.0, 0.0, 0.5, 0.5)))

    def test_pdf_with_offset_visible_area_matches_preview(self):
        def shrink(page):
            page.cropbox = editor.pypdf.generic.RectangleObject((20, 10, 180, 90))
        page = editor.pages_from_file(self._marked_pdf("c.pdf", shrink))[0]
        self.assertEqual(tuple(round(v) for v in editor.render_final(page, 72)[0].size), (160, 80))
        self.assert_export_matches_preview(page)
        self.assert_export_matches_preview(editor.replace(page, crop=(0.0, 0.0, 0.5, 0.5)))
        self.assert_export_matches_preview(editor.rotated(page, 90))

    def test_retouched_pdf_page_becomes_an_image(self):
        page = editor.pages_from_file(self.vector_pdf(pages=1))[0]
        target = self.dir / "gray.pdf"
        editor.export_pdf([editor.replace(page, filter="gris")], target)
        self.assertEqual(len(editor.pypdf.PdfReader(target).pages[0].images), 1)

    def test_password_protects_the_file(self):
        page = editor.pages_from_file(self.vector_pdf(pages=1))[0]
        target = self.dir / "locked.pdf"
        editor.export_pdf([page], target, password="secret")
        reader = editor.pypdf.PdfReader(target)
        self.assertTrue(reader.is_encrypted)
        self.assertNotEqual(reader.decrypt("mauvais"), editor.pypdf.PasswordType.OWNER_PASSWORD)
        self.assertEqual(reader.decrypt("mauvais").name, "NOT_DECRYPTED")
        reader = editor.pypdf.PdfReader(target)
        self.assertNotEqual(reader.decrypt("secret").name, "NOT_DECRYPTED")
        self.assertIn("page0", reader.pages[0].extract_text())

    def test_compact_quality_is_lighter_and_keeps_page_size(self):
        import numpy as np
        noise = np.random.default_rng(1).integers(120, 255, (2000, 1400, 3), dtype=np.uint8)
        page = editor.page_from_scan(Image.fromarray(noise), 300, self.dir, 1)
        high, low = self.dir / "high.pdf", self.dir / "low.pdf"
        editor.export_pdf([page], high)
        editor.export_pdf([page], low, quality="Compacte")
        self.assertLess(low.stat().st_size, high.stat().st_size / 3)
        self.assertAlmostEqual(self.sizes(low)[0][0], self.sizes(high)[0][0], delta=0.5)

    def test_vector_crop_keeps_hidden_text_but_ocr_removes_it(self):
        """Documente la limite annoncée : un recadrage vectoriel masque sans effacer."""
        page = editor.pages_from_file(self.vector_pdf(pages=1))[0]
        hidden = editor.replace(page, crop=(0.8, 0.8, 0.2, 0.2))
        target = self.dir / "hidden.pdf"
        editor.export_pdf([hidden], target)
        self.assertIn("page0", editor.pypdf.PdfReader(target).pages[0].extract_text())
        flattened = self.dir / "flat.pdf"
        with mock.patch.object(documents, "image_to_ocr_pdf", lambda image, path, dpi, lang: documents.pages_to_pdf([image], path, dpi)):
            editor.export_pdf([hidden], flattened, ocr_lang="fra")
        self.assertNotIn("page0", editor.pypdf.PdfReader(flattened).pages[0].extract_text())

    def test_unwritable_folder_gives_a_clear_error(self):
        page = editor.pages_from_file(self.vector_pdf(pages=1))[0]
        with self.assertRaisesRegex(RuntimeError, "Impossible d'enregistrer"):
            editor.export_pdf([page], self.dir / "absent" / "out.pdf")

    def test_failed_export_leaves_no_file(self):
        page = Page(source=self.dir / "absent.png", uid=1)
        target = self.dir / "out.pdf"
        with self.assertRaises(Exception):
            editor.export_pdf([page], target)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
