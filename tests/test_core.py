import tempfile
import unittest
from pathlib import Path

from PIL import Image

from imprimerie import brother, devices, documents, escl, printing, sane_scan


class BrotherTests(unittest.TestCase):
    def setUp(self):
        self.label = brother.LABELS_BY_ID["102x152"]

    def test_media_detection(self):
        self.assertEqual(brother.label_from_media('102mm x 152mm / 4" x 6"').id, "102x152")
        self.assertEqual(brother.label_from_media("62mm").id, "62")
        self.assertIsNone(brother.label_from_media("inconnu"))
        self.assertIsNone(brother.label_from_media(None))

    def test_fit_landscape_is_rotated_without_distortion(self):
        fitted = brother.fit_to_label(Image.new("RGB", (2000, 1000), "black"), self.label)
        self.assertEqual(fitted.size, (1164, 1660))
        # 2000x1000 tourné donne 1000x2000 : mis à l'échelle sur la hauteur, avec des marges blanches.
        self.assertEqual(fitted.getpixel((10, 800)), 255)
        self.assertEqual(fitted.getpixel((582, 800)), 0)

    def test_transparency_becomes_white(self):
        fitted = brother.fit_to_label(Image.new("RGBA", (100, 100), (0, 0, 0, 0)), self.label)
        self.assertEqual(fitted.getpixel((582, 830)), 255)

    def test_frame_structure(self):
        bitmap = brother.to_bitmap(Image.new("L", (1164, 1660), 255))
        data = brother.build_job([bitmap], self.label)
        self.assertTrue(data.startswith(b"\x1b\x69\x61\x01" + b"\x00" * 200 + b"\x1b\x40\x1b\x69\x61\x01"))
        self.assertEqual(data[-1:], b"\x1a")
        self.assertIn(b"\x1b\x69\x7a\xce\x0b\x66\x99\x7c\x06\x00\x00\x00\x00", data)
        self.assertEqual(data.count(b"\x67\x00\xa2" + b"\x00" * 162), 1660)

    def test_black_prints_at_mirrored_position(self):
        image = Image.new("L", (1164, 1660), 255)
        image.paste(0, (0, 0, 8, 1660))    # bande noire à gauche de l'image
        data = brother.build_job([brother.to_bitmap(image)], self.label)
        rows = data[data.index(b"\x67\x00\xa2"):]
        row = rows[3:165]
        # La marge image est de 76 points : la bande reste à 76 points du bord droit du raster.
        lit = [i for i in range(1296) if row[i // 8] >> (7 - i % 8) & 1]
        self.assertEqual(lit, list(range(1296 - 76 - 8, 1296 - 76)))

    def test_multi_label_separator(self):
        bitmap = brother.to_bitmap(Image.new("L", (1164, 526), 255))
        data = brother.build_job([bitmap, bitmap], brother.LABELS_BY_ID["102x51"])
        self.assertEqual(data[-1:], b"\x1a")
        self.assertEqual(data.count(b"\x1b\x69\x7a"), 2)

    def test_wrong_size_refused(self):
        with self.assertRaises(ValueError):
            brother.raster_frame(Image.new("1", (100, 100)), self.label)


class SendTests(unittest.TestCase):
    def test_send_delivers_all_bytes(self):
        import socket
        import threading
        server = socket.create_server(("127.0.0.1", 0))
        received = bytearray()

        def serve():
            connection, _ = server.accept()
            with connection:
                while chunk := connection.recv(65536):
                    received.extend(chunk)

        thread = threading.Thread(target=serve)
        thread.start()
        original, brother.PORT = brother.PORT, server.getsockname()[1]
        try:
            brother.send(b"x" * 300000, "127.0.0.1")
        finally:
            brother.PORT = original
        thread.join(5)
        server.close()
        self.assertEqual(len(received), 300000)


class PrintingTests(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(printing.expand_ranges("1-3, 5", 10), [0, 1, 2, 4])
        self.assertEqual(printing.expand_ranges("", 2), [0, 1])
        self.assertEqual(printing.expand_ranges("9", 2), [])
        with self.assertRaises(ValueError):
            printing.expand_ranges("a", 2)

    def test_lp_args(self):
        args = printing.lp_args("Q", "-weird.pdf", "T", 2, "1-2", {"Duplex": "None"}, True, "paysage")
        self.assertEqual(args[-2:], ["--", "-weird.pdf"])
        for value in ("-n", "2", "-P", "1-2", "Duplex=None", "fit-to-page", "orientation-requested=4"):
            self.assertIn(value, args)

    def test_parse_options(self):
        text = "PageSize/Media Size: A5 *A4 Custom.WIDTHxHEIGHT\nDuplex/Duplex: *None DuplexTumble\nOutputBin/O: *FaceUp"
        options = printing.parse_options(text)
        self.assertEqual([o.key for o in options], ["PageSize", "Duplex"])
        self.assertEqual(options[0].values, ["A5", "A4"])
        self.assertEqual(options[0].default, "A4")
        self.assertEqual(printing.value_label("A4.Borderless"), "A4 sans marges")


class ScanTests(unittest.TestCase):
    def test_capabilities(self):
        xml = (b'<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" '
               b'xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"><pwg:MakeAndModel>E</pwg:MakeAndModel>'
               b'<scan:Platen><scan:PlatenInputCaps><scan:MaxWidth>2550</scan:MaxWidth>'
               b'<scan:DiscreteResolution><scan:XResolution>300</scan:XResolution></scan:DiscreteResolution>'
               b'<scan:DiscreteResolution><scan:XResolution>100</scan:XResolution></scan:DiscreteResolution>'
               b'</scan:PlatenInputCaps></scan:Platen></scan:ScannerCapabilities>')
        caps = escl.parse_capabilities(xml)
        self.assertEqual(caps.model, "E")
        self.assertEqual(caps.resolutions, [100, 300])

    def test_scan_command(self):
        args = sane_scan.scan_args("1.2.3.4", 300, "Color", "A4", "/tmp/o.png")
        self.assertEqual(args[2], "epsonscan2:networkscanner:esci2:network:1.2.3.4")
        for value in ("Flatbed", "Color", "300", "A4", "--format=png", "/tmp/o.png"):
            self.assertIn(value, args)

    def test_busy_error_points_to_the_screen(self):
        message = sane_scan.explain("scanimage: sane_start: Document feeder out of documents")
        self.assertIn("écran", message)


class DesktopTests(unittest.TestCase):
    def test_reveal_falls_back_to_the_parent_folder(self):
        from unittest import mock
        from imprimerie import desktop
        target = Path("/tmp/dossier/fichier.pdf")
        with mock.patch.object(desktop, "_show_selected", return_value=False), \
                mock.patch.object(desktop.QDesktopServices, "openUrl", return_value=True) as opened:
            self.assertTrue(desktop.reveal_file(target))
        self.assertEqual(opened.call_args[0][0].toLocalFile(), "/tmp/dossier")

    def test_reveal_does_not_open_the_folder_twice(self):
        from unittest import mock
        from imprimerie import desktop
        with mock.patch.object(desktop, "_show_selected", return_value=True), \
                mock.patch.object(desktop.QDesktopServices, "openUrl") as opened:
            self.assertTrue(desktop.reveal_file(Path("/tmp/a.pdf")))
        opened.assert_not_called()


class DeviceTests(unittest.TestCase):
    def test_parse_ipp(self):
        out = "   printer-state (enum) = idle\n   marker-levels (1setOf integer) = 88,84\n"
        self.assertEqual(devices.parse_ipp(out), {"printer-state": "idle", "marker-levels": "88,84"})


class DocumentTests(unittest.TestCase):
    def test_crop(self):
        cropped = documents.crop_normalized(Image.new("RGB", (100, 200)), (0.5, 0.5, 0.5, 0.25))
        self.assertEqual(cropped.size, (50, 50))
        with self.assertRaises(ValueError):
            documents.crop_normalized(Image.new("RGB", (100, 200)), (0, 0, 0, 0))

    def test_pdf_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.pdf"
            documents.pages_to_pdf([Image.new("RGB", (200, 300), "white")] * 2, target, 100)
            document = documents.Document(target)
            self.assertEqual(document.page_count, 2)
            self.assertEqual(document.render(1, 100).size[0], 200)

    def test_pdf_with_black_and_white_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            pure = Path(tmp) / "pure.pdf"
            documents.pages_to_pdf([Image.new("1", (200, 300), 1)], pure, 100)
            self.assertEqual(documents.Document(pure).page_count, 1)
            mixed = Path(tmp) / "mixed.pdf"
            documents.pages_to_pdf([Image.new("L", (200, 300), 255), Image.new("1", (200, 300), 1)], mixed, 100)
            self.assertEqual(documents.Document(mixed).page_count, 2)

    def test_missing_and_unknown(self):
        with self.assertRaises(documents.DocumentError):
            documents.Document("/inexistant.pdf")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.bin"
            path.write_bytes(b"\xff\xfe\x00\x80\x81")
            with self.assertRaises(documents.DocumentError):
                documents.Document(path)


if __name__ == "__main__":
    unittest.main()
