"""Ajout des appareils : droits, détection, options de scan."""
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from feuillet import admin, config, devices, discovery, escl, printing, sane_scan

EPSONSCAN_OPTIONS = """Options specific to device `epsonscan2:x':
    --mode Color|Grayscale|Monochrome [Color]
    --source Flatbed|Flatbed [Flatbed]
    --resolution 50..1200dpi (in steps of 1) [600]
    --scan-area Letter|A4|A5|A5 (Landscape)|A6|Maximum|Manual [Manual]
    -l 0..215.9mm [0]
    -x 0..215.9mm [215.9]
    -y 0..297.18mm [297.18]
"""
GENERIC_OPTIONS = """  Scan mode:
    --mode Lineart|Gray|Color [Lineart]
    --resolution 75|150|300|600dpi [300]
    --source Flatbed|ADF [Flatbed]
  Geometry:
    -l 0..216mm [0]
    -t 0..200mm [0]
    -x 0..216mm [216]
    -y 0..200mm [200]
"""


class AdminTests(unittest.TestCase):
    def fake(self, name, code, stderr=""):
        path = Path(self.tmp.name) / name
        path.write_text(f"#!/bin/sh\ncat > {self.tmp.name}/stdin.txt\necho \"$@\" > {self.tmp.name}/args.txt\n"
                        f"echo '{stderr}' >&2\nexit {code}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"PATH": f"{self.tmp.name}:/usr/bin:/bin"})
        self.env.start()
        self.root = mock.patch.object(os, "geteuid", return_value=1000)
        self.root.start()

    def tearDown(self):
        self.root.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_missing_agent_asks_for_the_password(self):
        self.fake("pkexec", 127, "Error: No authentication agent found.")
        with self.assertRaises(admin.NeedPassword):
            admin.run_admin(["lpadmin", "-x", "Q"])

    def test_dismissed_dialog_is_a_cancellation(self):
        self.fake("pkexec", 126, "Not authorized")
        with self.assertRaises(admin.AdminCancelled):
            admin.run_admin(["lpadmin", "-x", "Q"])

    def test_password_goes_only_through_stdin(self):
        self.fake("sudo", 0)
        admin.run_admin(["lpadmin", "-x", "Q"], "m0t de passe")
        self.assertEqual(Path(self.tmp.name, "stdin.txt").read_text(), "m0t de passe\n")
        arguments = Path(self.tmp.name, "args.txt").read_text()
        self.assertNotIn("m0t", arguments)
        self.assertIn("-S -k", arguments)
        self.assertIn("-- lpadmin -x Q", arguments)

    def test_wrong_password(self):
        self.fake("sudo", 1, "Sorry, try again.")
        with self.assertRaises(admin.WrongPassword):
            admin.run_admin(["true"], "faux")

    def test_account_without_rights(self):
        self.fake("sudo", 1, "moi is not in the sudoers file.")
        with self.assertRaisesRegex(admin.AdminError, "droit"):
            admin.run_admin(["true"], "x")


class AddressTests(unittest.TestCase):
    def test_addresses(self):
        self.assertEqual(discovery.split_address(" 192.168.0.20 "), ("192.168.0.20", None, None))
        self.assertEqual(discovery.split_address("hote:8631"), ("hote", 8631, None))
        self.assertEqual(discovery.split_address("ipp://hote:8631/ipp/print"), ("hote", 8631, "ipp/print"))
        self.assertEqual(discovery.host_of_uri("ipp://192.168.0.20/ipp/print"), "192.168.0.20")

    def test_queue_name_is_valid_for_cups(self):
        self.assertEqual(discovery.queue_name("EPSON XP-6000 Series"), "EPSON_XP-6000_Series")
        self.assertEqual(discovery.queue_name("HP/LaserJet #4"), "HP_LaserJet_4")
        self.assertEqual(discovery.queue_name("///"), "Imprimante")


class ClassificationTests(unittest.TestCase):
    def test_driverless_detection(self):
        self.assertTrue(discovery.supports_driverless({"document-format-supported": "application/pdf,image/urf"}))
        self.assertTrue(discovery.supports_driverless({"ipp-features-supported": "airprint-1.7"}))
        self.assertFalse(discovery.supports_driverless({"document-format-supported": "application/postscript"}))

    def inspect(self, attributes, driver=""):
        with mock.patch.object(devices, "ipp_attributes", lambda uri: attributes if uri.endswith("/ipp/print") else {}), \
                mock.patch.object(discovery, "find_driver", lambda _m: driver), \
                mock.patch.object(discovery, "has_escl", lambda _h: False):
            return discovery.inspect_address("10.0.0.5", [])

    def test_airprint_printer_needs_no_driver(self):
        found = self.inspect({"printer-state": "idle", "printer-make-and-model": "HP Laser", "document-format-supported": "image/pwg-raster"})
        self.assertEqual((found.method, found.uri, found.name), ("everywhere", "ipp://10.0.0.5:631/ipp/print", "HP Laser"))

    def test_old_ipp_printer_uses_an_installed_driver(self):
        found = self.inspect({"printer-state": "idle", "printer-make-and-model": "Vieille", "document-format-supported": "application/postscript"}, "drv:///x.ppd")
        self.assertEqual((found.method, found.driver), ("driver", "drv:///x.ppd"))

    def test_no_driver_is_said_clearly(self):
        found = self.inspect({"printer-state": "idle", "printer-make-and-model": "Vieille", "document-format-supported": "application/postscript"})
        self.assertFalse(found.can_print)
        self.assertIn("pilote", found.note)

    def test_scanner_prefers_the_vendor_driver(self):
        device = "epsonscan2:networkscanner:esci2:network:10.0.0.5"
        scanner = discovery.scanner_for_host("10.0.0.5", "Epson", [(device, "x")])
        self.assertEqual((scanner["backend"], scanner["device"]), ("sane", device))
        with mock.patch.object(discovery, "has_escl", return_value=True):
            other = discovery.scanner_for_host("10.0.0.50", "Epson", [(device, "x")])
        self.assertEqual(other["backend"], "escl")

    def test_existing_queue_is_recognized(self):
        item = discovery.Found(name="X", host="10.0.0.5", uri="ipp://10.0.0.5:631/ipp/print", method="everywhere")
        discovery.mark_configured([item], {"X_q": {"device-uri": "ipp://10.0.0.5/ipp/print"}}, "")
        self.assertEqual((item.configured, item.queue), (True, "X_q"))

    def test_changed_address_is_detected(self):
        item = discovery.Found(name="EPSON XP", host="10.0.0.9", uri="ipp://10.0.0.9:631/ipp/print", method="everywhere")
        queues = {"EPSON_XP": {"device-uri": "ipp://10.0.0.5/ipp/print", "printer-make-and-model": "EPSON XP driverless"}}
        with mock.patch.object(discovery, "_alive", return_value=False):
            discovery.mark_configured([item], queues, "")
        self.assertTrue(item.update_uri)
        self.assertFalse(item.configured)
        self.assertEqual(item.queue, "EPSON_XP")

    def test_other_printer_is_not_taken_for_a_moved_one(self):
        item = discovery.Found(name="Canon", host="10.0.0.9", uri="ipp://10.0.0.9:631/ipp/print", method="everywhere")
        queues = {"EPSON_XP": {"device-uri": "ipp://10.0.0.5/ipp/print", "printer-make-and-model": "EPSON XP"}}
        with mock.patch.object(discovery, "_alive", return_value=False):
            discovery.mark_configured([item], queues, "")
        self.assertFalse(item.update_uri)


class ScanOptionTests(unittest.TestCase):
    def test_epson_options(self):
        caps = sane_scan.parse_options(EPSONSCAN_OPTIONS)
        self.assertEqual(caps.modes, {"Couleur": "Color", "Niveaux de gris": "Grayscale"})
        self.assertEqual(caps.resolutions, [100, 150, 200, 300, 600, 1200])
        self.assertTrue(caps.named_areas)
        args = sane_scan.scan_args("d", caps, 300, "Couleur", "A4", "/o.png")
        self.assertEqual(args[args.index("--scan-area") + 1], "A4")
        self.assertNotIn("-x", args)

    def test_generic_options_use_dimensions_within_the_glass(self):
        caps = sane_scan.parse_options(GENERIC_OPTIONS)
        self.assertEqual(caps.resolutions, [75, 150, 300, 600])
        self.assertEqual(caps.sources, ["Flatbed", "ADF"])
        self.assertIn("Noir et blanc", caps.colors)
        self.assertNotIn("A4", caps.areas)          # la vitre ne fait que 200 mm de haut
        self.assertIn("A6", caps.areas)
        args = sane_scan.scan_args("d", caps, 150, "Noir et blanc", "A6", "/o.png", "ADF")
        self.assertEqual(args[args.index("--mode") + 1], "Gray")
        self.assertEqual(args[args.index("-x") + 1], "105")
        self.assertEqual(args[args.index("--source") + 1], "ADF")
        full = sane_scan.scan_args("d", caps, 150, "Couleur", sane_scan.FULL_AREA, "/o.png")
        self.assertNotIn("-x", full)


class FakeScanner(BaseHTTPRequestHandler):
    jpeg = b""
    posts: list = []
    busy = 0
    stuck = False
    deleted: list = []

    def log_message(self, *_args):
        pass

    def _send(self, code, body=b"", headers=()):
        self.send_response(code)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("ScannerCapabilities"):
            self._send(200, (b'<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" '
                             b'xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"><pwg:MakeAndModel>Faux</pwg:MakeAndModel>'
                             b'<scan:Platen><scan:PlatenInputCaps><scan:MaxWidth>2550</scan:MaxWidth><scan:MaxHeight>3300</scan:MaxHeight>'
                             b'<scan:DiscreteResolution><scan:XResolution>300</scan:XResolution></scan:DiscreteResolution>'
                             b'</scan:PlatenInputCaps></scan:Platen></scan:ScannerCapabilities>'))
        elif self.path.endswith("NextDocument"):
            if FakeScanner.stuck or FakeScanner.busy > 0:
                FakeScanner.busy -= 1
                self._send(503, headers=[("Retry-After", "0")])
            else:
                self._send(200, FakeScanner.jpeg)
        else:
            self._send(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        FakeScanner.posts.append(self.rfile.read(length))
        self._send(201, headers=[("Location", "/eSCL/ScanJobs/42")])

    def do_DELETE(self):
        FakeScanner.deleted.append(self.path)
        self._send(200)


class EsclTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from io import BytesIO
        from PIL import Image
        stream = BytesIO()
        Image.new("RGB", (30, 40), "white").save(stream, "JPEG")
        FakeScanner.jpeg = stream.getvalue()
        cls.server = HTTPServer(("127.0.0.1", 0), FakeScanner)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.host = f"127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeScanner.posts, FakeScanner.deleted, FakeScanner.busy, FakeScanner.stuck = [], [], 0, False
        self.sleep = mock.patch.object(escl.time, "sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def client(self):
        return escl.Scanner(self.host, "http")

    def test_scan_waits_then_delivers_without_cancelling(self):
        FakeScanner.busy = 2
        data = self.client().scan(300, "RGB24", (2480, 3508))
        self.assertEqual(data, FakeScanner.jpeg)
        self.assertEqual(FakeScanner.deleted, [])

    def test_area_is_limited_to_the_glass(self):
        self.client().scan(300, "RGB24", (2480, 3508))
        body = FakeScanner.posts[0].decode()
        self.assertIn("<pwg:Height>3300</pwg:Height>", body)
        self.assertIn("<pwg:Width>2480</pwg:Width>", body)

    def test_stuck_scan_is_cancelled_with_a_clear_message(self):
        FakeScanner.stuck = True
        with mock.patch.object(escl, "STALL_TIMEOUT", 0.05):
            with self.assertRaisesRegex(escl.ScanError, "écran"):
                self.client().scan(300, "RGB24", None)
        self.assertEqual(FakeScanner.deleted, ["/eSCL/ScanJobs/42"])

    def test_image_through_the_generic_interface(self):
        scanner = {"id": "x", "name": "x", "backend": "escl", "device": "", "host": self.host}
        with mock.patch.object(escl.Scanner, "__init__", lambda s, host, scheme="": (setattr(s, "host", host), setattr(s, "scheme", "http"), setattr(s, "_context", None))[0]):
            image = sane_scan.scan(scanner, sane_scan.Capabilities(), 300, "Couleur", "A4")
        self.assertEqual(image.size, (30, 40))


class ConfigMigrationTests(unittest.TestCase):
    def test_old_epson_address_keeps_its_scanner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"epson_host": "192.168.0.20", "brother_host": "192.168.0.35", "theme": "clair"}))
            with mock.patch.object(config, "CONFIG_PATH", path):
                data = config.load()
        self.assertEqual(data["brother_host"], "192.168.0.35")
        self.assertEqual(data["scanners"][0]["device"], "epsonscan2:networkscanner:esci2:network:192.168.0.20")
        self.assertNotIn("epson_host", data)

    def test_new_install_has_no_device(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "CONFIG_PATH", Path(tmp) / "absent.json"):
            data = config.load()
        self.assertEqual((data["brother_host"], data["scanners"]), ("", []))


class PrintingAdminTests(unittest.TestCase):
    def test_user_choice_of_default_wins_over_the_system(self):
        class Dest:
            def __init__(self, default):
                self.is_default = default
        class Conn:
            def getDests(self):
                return {("A", None): Dest(False), ("B", None): Dest(True), (None, None): Dest(True)}
            def getDefault(self):
                return "A"
        with mock.patch.object(printing.cups, "Connection", Conn):
            self.assertEqual(printing.default_queue(), "B")

    def capture(self):
        calls = []
        return calls, mock.patch.object(printing.admin, "run_admin", lambda args, password=None: calls.append((args, password)))

    def test_add_queue_uses_driverless_model(self):
        calls, patch = self.capture()
        with patch:
            printing.add_queue("Q", "ipp://h/ipp/print", "everywhere", "Ma Q", "pw")
        self.assertEqual(calls, [(["lpadmin", "-p", "Q", "-E", "-v", "ipp://h/ipp/print", "-m", "everywhere", "-D", "Ma Q"], "pw")])

    def test_address_change_keeps_the_driver(self):
        calls, patch = self.capture()
        with patch:
            printing.update_queue_uri("Q", "ipp://n/ipp/print")
        self.assertNotIn("-m", calls[0][0])


if __name__ == "__main__":
    unittest.main()
