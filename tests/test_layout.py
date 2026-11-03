"""Aucun contrôle ne doit se chevaucher, déborder ni couper son texte, quelle que soit la largeur.

Un gestionnaire de fenêtres en mosaïque impose sa taille et ignore le minimum de la fenêtre.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLineEdit, QWidget  # noqa: E402

from feuillet import config, devices, editor, escl, printing, sane_scan, theme, ui  # noqa: E402

QUEUE = {"EPSON_XP-6000_Series": {"printer-info": "EPSON_XP-6000_Series", "device-uri": "ipp://192.168.0.20/ipp/print",
                                  "printer-make-and-model": "EPSON"}}
OPTIONS = ("PageSize/Media Size: A5 *A4 A4.Borderless Letter.Borderless\nMediaType/Media Type: *Stationery Photographic\n"
           "InputSlot/Source: *Main Auto\nColorModel/Mode: *RGB Gray\nDuplex/Duplex: *None DuplexTumble\n"
           "cupsPrintQuality/Q: Draft *Normal High")
SIZES = [(1360, 860), (1100, 760), (1099, 760), (900, 700), (790, 972), (760, 600), (759, 600), (640, 600), (520, 500)]


def rect_in_window(widget: QWidget, window: QWidget) -> QRect:
    return QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())


class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.patches = [
            mock.patch.object(config, "CONFIG_PATH", base / "config.json"),
            mock.patch.object(editor, "data_dir", lambda: base / "session"),
            mock.patch.object(printing, "list_queues", lambda: QUEUE),
            mock.patch.object(printing, "default_queue", lambda: "EPSON_XP-6000_Series"),
            mock.patch.object(printing, "queue_options", lambda _q: printing.parse_options(OPTIONS)),
            mock.patch.object(printing, "list_jobs", lambda: []),
            mock.patch.object(devices, "queue_status", lambda _u: devices.Status(online=True, state="Prête", ink=[("Noir", "#000000", 50)])),
            mock.patch.object(printing, "service_running", lambda: True),
            mock.patch.object(devices, "brother_status", lambda _h: devices.Status(online=True, state="Prête")),
            mock.patch.object(sane_scan, "capabilities", lambda _s: sane_scan.Capabilities()),
            mock.patch.object(escl.Scanner, "state", lambda _s: "Idle"),
        ]
        (base / "config.json").write_text(json.dumps({
            "brother_host": "192.168.0.35",
            "scanners": [{"id": "a", "name": "Scanner A", "backend": "sane", "device": "a", "host": ""},
                         {"id": "b", "name": "Scanner B au nom vraiment très long", "backend": "escl", "device": "", "host": "10.0.0.2"}]}))
        for patch in cls.patches:
            patch.start()
        theme.set_theme("sombre")
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(theme.build_qss())
        cls.window = ui.MainWindow()
        cls.window.setMinimumSize(0, 0)     # comme en mosaïque : la taille est imposée
        cls.window.poll.stop()
        cls.window.status_timer.stop()
        cls.window.show()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        for patch in cls.patches:
            patch.stop()
        cls.tmp.cleanup()

    def settle(self):
        for _ in range(5):
            self.app.processEvents()

    def check(self, where: str):
        self.settle()
        window = self.window
        controls = [w for w in window.findChildren(QWidget)
                    if isinstance(w, (QAbstractButton, QComboBox, QLineEdit)) and w.isVisible()
                    and not w.objectName().startswith("qt_") and w.window() is window
                    and type(w.parent()).__name__ not in ("QComboBox",) and "QSpinBox" not in type(w.parent()).__name__]
        scrolled = {id(w): self._inside_scroll(w) for w in controls}
        for widget in controls:
            rect = rect_in_window(widget, window)
            name = widget.objectName() or getattr(widget, "text", lambda: "")() or type(widget).__name__
            if not scrolled[id(widget)]:
                self.assertLessEqual(rect.right(), window.width() + 1, f"{where} : « {name} » sort à droite")
                self.assertLessEqual(rect.bottom(), window.height() + 1, f"{where} : « {name} » sort en bas")
            self.assertGreaterEqual(rect.left(), -1, f"{where} : « {name} » sort à gauche")
            if isinstance(widget, QAbstractButton) and widget.text() and widget.objectName() != "sectionHeader":
                self.assertGreaterEqual(widget.width() + 3, widget.fontMetrics().horizontalAdvance(widget.text()),
                                        f"{where} : texte coupé sur « {widget.text()} »")
        # Les panneaux ne se recouvrent pas.
        for page in (window.print_page, window.scan_page):
            if window.stack.currentWidget() is page:
                panels = [w for w in (getattr(page, "strip_panel", None), getattr(page, "files_panel", None),
                                      page.center_widget, page.side_scroll) if w is not None and w.isVisible()]
                for index, first in enumerate(panels):
                    for second in panels[index + 1:]:
                        overlap = rect_in_window(first, window).intersected(rect_in_window(second, window))
                        self.assertTrue(overlap.isEmpty(), f"{where} : deux panneaux se recouvrent")
                for panel_widget in panels:
                    self.assertLessEqual(rect_in_window(panel_widget, window).right(), window.width() + 1,
                                         f"{where} : un panneau sort de la fenêtre")

    @staticmethod
    def _inside_scroll(widget: QWidget) -> bool:
        parent = widget.parent()
        while parent is not None:
            if type(parent).__name__ == "QScrollArea":
                return True
            parent = parent.parent()
        return False

    def test_every_page_at_every_size(self):
        for width, height in SIZES:
            self.window.resize(width, height)
            self.settle()
            self.assertEqual((self.window.width(), self.window.height()), (width, height))
            for index, name in enumerate(("Imprimer", "Scanner", "Travaux", "Réglages")):
                self.window.show_page(index)
                self.check(f"{name} à {width}×{height}")
                page = self.window.stack.currentWidget()
                if self.window.layout_mode == "etroit" and hasattr(page, "view_switch"):
                    page.view_switch.settings_button.setChecked(True)
                    self.check(f"{name}, réglages, à {width}×{height}")
                    page.view_switch.main_button.setChecked(True)

    def test_narrow_mode_keeps_settings_reachable(self):
        self.window.resize(640, 600)
        self.window.show_page(1)
        self.settle()
        page = self.window.scan_page
        self.assertTrue(page.center_widget.isVisible())
        self.assertFalse(page.side_scroll.isVisible())
        page.view_switch.settings_button.setChecked(True)
        self.settle()
        self.assertFalse(page.center_widget.isVisible())
        self.assertTrue(page.side_scroll.isVisible())
        page.view_switch.main_button.setChecked(True)
        self.window.resize(1360, 860)
        self.settle()
        self.assertTrue(page.side_scroll.isVisible())
        self.assertTrue(page.center_widget.isVisible())


if __name__ == "__main__":
    unittest.main()
