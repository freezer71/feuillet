"""Interface Qt : Imprimer, Scanner, Travaux, Appareils."""
from __future__ import annotations

import datetime as dt
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import brother, config, devices, documents, icons, printing, theme
from .editor_ui import EditorPage
from .widgets import Section, ToastHost, compact_combo, empty_state, label, panel, run_async, to_pixmap

class Preview(QWidget):
    """Affiche une page. Un glisser à la souris y sélectionne une zone, si la sélection est active."""
    selectionChanged = Signal()

    def __init__(self, hint: str = "", selectable: bool = False):
        super().__init__()
        self.setObjectName("preview")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumSize(320, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.hint, self.selectable = hint, selectable
        self.pixmap: QPixmap | None = None
        self.selection: QRectF | None = None
        self._anchor: QPointF | None = None
        if selectable:
            self.setCursor(Qt.CursorShape.CrossCursor)

    def set_image(self, pixmap: QPixmap | None, keep_selection: bool = False) -> None:
        self.pixmap = pixmap
        if not keep_selection:
            self.clear_selection()
        self.update()

    def clear_selection(self) -> None:
        if self.selection is not None:
            self.selection = None
            self.selectionChanged.emit()
        self.update()

    def page_rect(self) -> QRectF:
        if not self.pixmap:
            return QRectF()
        area = QRectF(self.rect()).adjusted(14, 14, -14, -14)
        size = self.pixmap.size().toSizeF().scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF(area.center().x() - size.width() / 2, area.center().y() - size.height() / 2,
                      size.width(), size.height())

    def _normalized(self, point: QPointF) -> QPointF:
        rect = self.page_rect()
        return QPointF(min(1.0, max(0.0, (point.x() - rect.x()) / rect.width())),
                       min(1.0, max(0.0, (point.y() - rect.y()) / rect.height())))

    def mousePressEvent(self, event):
        if self.selectable and self.pixmap and event.button() == Qt.MouseButton.LeftButton:
            self._anchor = self._normalized(event.position())
            self.selection = None
            self.update()

    def mouseMoveEvent(self, event):
        if self._anchor is not None:
            self.selection = QRectF(self._anchor, self._normalized(event.position())).normalized()
            self.update()

    def mouseReleaseEvent(self, event):
        if self._anchor is None:
            return
        self._anchor = None
        if self.selection is not None and (self.selection.width() < 0.02 or self.selection.height() < 0.02):
            self.selection = None
        self.selectionChanged.emit()
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not self.pixmap:
            empty_state(painter, self.rect(), "print", "Aucun document", self.hint)
            return
        rect = self.page_rect()
        painter.fillRect(rect, Qt.GlobalColor.white)
        painter.drawPixmap(rect, self.pixmap, QRectF(self.pixmap.rect()))
        if self.selection:
            zone = QRectF(rect.x() + self.selection.x() * rect.width(), rect.y() + self.selection.y() * rect.height(),
                          self.selection.width() * rect.width(), self.selection.height() * rect.height())
            shade = QColor(theme.color("shade"))
            painter.fillRect(QRectF(rect.x(), rect.y(), rect.width(), zone.y() - rect.y()), shade)
            painter.fillRect(QRectF(rect.x(), zone.bottom(), rect.width(), rect.bottom() - zone.bottom()), shade)
            painter.fillRect(QRectF(rect.x(), zone.y(), zone.x() - rect.x(), zone.height()), shade)
            painter.fillRect(QRectF(zone.right(), zone.y(), rect.right() - zone.right(), zone.height()), shade)
            painter.setPen(QPen(QColor(theme.color("primary")), 2))
            painter.drawRect(zone)


@dataclass
class Target:
    """Une imprimante proposée : une file CUPS, ou la Brother en direct."""
    id: str
    name: str
    is_brother: bool = False


class PrintPage(QWidget):
    message = Signal(str, str)

    def __init__(self, cfg: dict):
        super().__init__()
        self.setObjectName("page")
        self.cfg = cfg
        self.toasts = None
        self.docs: list[documents.Document] = []
        self.page_index = 0
        self.targets: list[Target] = []
        self.option_boxes: dict[str, QComboBox] = {}
        self.media_text = ""
        self._render_token = 0

        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        files, files_layout = panel()
        files.setFixedWidth(210)
        files_layout.addWidget(label("Documents", "title"))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._doc_selected)
        files_layout.addWidget(self.list, 1)
        add = QPushButton("Ajouter des fichiers…")
        add.clicked.connect(self.choose_files)
        remove = QPushButton("Retirer de la liste")
        remove.clicked.connect(self._remove_current)
        files_layout.addWidget(add)
        files_layout.addWidget(remove)
        root.addWidget(files)

        center = QVBoxLayout()
        center.setSpacing(10)
        self.preview = Preview("Glisse un fichier ici, ou clique sur « Ajouter des fichiers ». "
                               "PDF, images, texte, et formats Office si LibreOffice est installé.", selectable=True)
        self.preview.selectionChanged.connect(self._selection_changed)
        center.addWidget(self.preview, 1)
        nav = QHBoxLayout()
        self.prev_button = QPushButton("‹ Page précédente")
        self.next_button = QPushButton("Page suivante ›")
        self.page_label = label("", "muted")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prev_button.clicked.connect(lambda: self._go(-1))
        self.next_button.clicked.connect(lambda: self._go(1))
        nav.addWidget(self.prev_button)
        nav.addWidget(self.page_label, 1)
        nav.addWidget(self.next_button)
        center.addLayout(nav)
        self.zone_label = label("Astuce : trace un rectangle sur la page pour n'imprimer que cette zone.", "muted")
        self.zone_clear = QPushButton("Effacer la zone")
        self.zone_clear.clicked.connect(self.preview.clear_selection)
        self.zone_clear.hide()
        zone = QHBoxLayout()
        zone.addWidget(self.zone_label, 1)
        zone.addWidget(self.zone_clear)
        center.addLayout(zone)
        root.addLayout(center, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(300)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side, side_layout = panel()
        side_layout.addWidget(label("Impression", "title"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.printer = compact_combo()
        self.printer.currentIndexChanged.connect(self._printer_changed)
        form.addRow("Imprimante", self.printer)
        self.copies = QSpinBox()
        self.copies.setRange(1, 99)
        self.copies.lineEdit().setTextMargins(6, 0, 0, 0)
        form.addRow("Copies", self.copies)
        self.pages = QLineEdit()
        self.pages.setPlaceholderText("Toutes (ex. 1-3, 5)")
        form.addRow("Pages", self.pages)
        side_layout.addLayout(form)
        self.printer_state = label("", "muted")
        side_layout.addWidget(self.printer_state)

        self.cups_widget = QWidget()
        self.cups_form = QFormLayout(self.cups_widget)
        self.cups_form.setContentsMargins(0, 0, 0, 0)
        self.cups_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        side_layout.addWidget(self.cups_widget)

        self.label_widget = QWidget()
        label_form = QFormLayout(self.label_widget)
        label_form.setContentsMargins(0, 0, 0, 0)
        label_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.label_box = compact_combo()
        for item in brother.LABELS:
            self.label_box.addItem(item.name, item.id)
        label_form.addRow("Étiquette", self.label_box)
        self.threshold = QSlider(Qt.Orientation.Horizontal)
        self.threshold.setRange(30, 90)
        self.threshold.setValue(70)
        label_form.addRow("Encrage", self.threshold)
        self.dither = QCheckBox("Tramage (photos, pas de code-barres)")
        label_form.addRow(self.dither)
        self.label_preview_button = QPushButton("Voir le résultat sur l'étiquette")
        self.label_preview_button.clicked.connect(self._show_label_preview)
        label_form.addRow(self.label_preview_button)
        side_layout.addWidget(self.label_widget)

        self.orientation = compact_combo()
        self.orientation.addItem("Automatique", "")
        self.orientation.addItem("Portrait", "portrait")
        self.orientation.addItem("Paysage", "paysage")
        self.fit = QCheckBox("Ajuster à la page")
        self.fit.setChecked(True)
        self.layout_widget = QWidget()
        layout_form = QFormLayout(self.layout_widget)
        layout_form.setContentsMargins(0, 0, 0, 0)
        layout_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout_form.addRow("Orientation", self.orientation)
        layout_form.addRow(self.fit)
        side_layout.addWidget(self.layout_widget)

        advanced = QWidget()
        advanced_form = QFormLayout(advanced)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.number_up = compact_combo()
        for value in ("1", "2", "4", "6", "9", "16"):
            self.number_up.addItem(f"{value} par feuille", value)
        self.page_set = compact_combo()
        self.page_set.addItem("Toutes", "")
        self.page_set.addItem("Impaires", "odd")
        self.page_set.addItem("Paires", "even")
        self.scaling = QSpinBox()
        self.scaling.setRange(10, 400)
        self.scaling.setValue(100)
        self.scaling.setSuffix(" %")
        self.scaling.lineEdit().setTextMargins(6, 0, 0, 0)
        self.reverse = QCheckBox("Dernière page en premier")
        self.collate = QCheckBox("Assembler les copies")
        self.collate.setChecked(True)
        self.mirror = QCheckBox("En miroir (transferts)")
        advanced_form.addRow("Pages sur la feuille", self.number_up)
        advanced_form.addRow("Sélection", self.page_set)
        advanced_form.addRow("Échelle", self.scaling)
        advanced_form.addRow(self.reverse)
        advanced_form.addRow(self.collate)
        advanced_form.addRow(self.mirror)
        self.advanced_section = Section("Options avancées", advanced, opened=False)
        side_layout.addWidget(self.advanced_section)

        side_layout.addStretch(1)
        self.print_button = QPushButton("Imprimer")
        self.print_button.setObjectName("primary")
        self.print_button.clicked.connect(self.print_now)
        icons.set_icon(self.print_button, "print", 18, "on_primary")
        side_layout.addWidget(self.print_button)
        scroll.setWidget(side)
        root.addWidget(scroll)

        self.refresh_printers()
        self._update_controls()

    def refresh_printers(self) -> None:
        current = self.printer.currentData() or self.cfg.get("last_printer")
        self.targets = []
        try:
            queues = printing.list_queues()
            default = printing.default_queue()
        except RuntimeError:
            queues, default = {}, None
            self.printer_state.setObjectName("error")
            self.printer_state.setText("CUPS ne répond pas.")
        for name, info in queues.items():
            if not printing.is_raw_brother_queue(name, info):
                title = info.get("printer-info") or name
                self.targets.append(Target(name, title.replace("_", " ")))
        self.targets.append(Target("__brother__", "Brother QL-1060N", True))
        self.printer.blockSignals(True)
        self.printer.clear()
        for target in self.targets:
            self.printer.addItem(target.name, target.id)
        wanted = current if current in [t.id for t in self.targets] else default
        index = self.printer.findData(wanted)
        self.printer.setCurrentIndex(max(0, index))
        self.printer.blockSignals(False)
        self._printer_changed()

    def target(self) -> Target | None:
        index = self.printer.currentIndex()
        return self.targets[index] if 0 <= index < len(self.targets) else None

    def _printer_changed(self) -> None:
        target = self.target()
        if not target:
            return
        self.cfg["last_printer"] = target.id
        config.save(self.cfg)
        self.label_widget.setVisible(target.is_brother)
        self.cups_widget.setVisible(not target.is_brother)
        self.layout_widget.setVisible(not target.is_brother)
        self.advanced_section.setVisible(not target.is_brother)
        self.printer_state.setObjectName("muted")
        self.printer_state.setText("Vérification…")
        self.printer_state.style().polish(self.printer_state)
        if target.is_brother:
            run_async(self, devices.brother_status, self.cfg["brother_host"],
                      done=self._brother_ready, failed=self._state_failed)
        else:
            run_async(self, printing.queue_options, target.id, done=self._fill_cups_options, failed=self._state_failed)
            run_async(self, devices.ipp_status, self.cfg["epson_host"], done=self._epson_ready, failed=self._state_failed)

    def _set_state(self, text: str, kind: str) -> None:
        self.printer_state.setObjectName(kind)
        self.printer_state.setText(text)
        self.printer_state.style().polish(self.printer_state)

    def _state_failed(self, message: str) -> None:
        self._set_state(message, "error")

    def _epson_ready(self, status: devices.Status) -> None:
        target = self.target()
        if not target or target.is_brother or "EPSON" not in target.id.upper():
            self._set_state("", "muted")
            return
        if status.online:
            self._set_state(status.state, "ok")
        else:
            self._set_state("Injoignable. Va dans « Appareils » pour la rechercher.", "error")

    def _brother_ready(self, status: devices.Status) -> None:
        target = self.target()
        if not target or not target.is_brother:
            return
        self.media_text = status.media
        detected = brother.label_from_media(status.media)
        if detected:
            self.label_box.setCurrentIndex(self.label_box.findData(detected.id))
        if not status.online:
            self._set_state("Injoignable. Va dans « Appareils » pour la rechercher.", "error")
        elif status.detail:
            self._set_state(f"{status.state} : {status.detail}", "warn")
        else:
            media = f" — rouleau {status.media.split(' /')[0]}" if status.media else ""
            self._set_state(f"{status.state}{media}", "ok")

    def _fill_cups_options(self, options: list[printing.Option]) -> None:
        target = self.target()
        if not target or target.is_brother:
            return
        while self.cups_form.rowCount():
            self.cups_form.removeRow(0)
        self.option_boxes = {}
        for option in options:
            box = compact_combo()
            for value in option.values:
                box.addItem(printing.value_label(value), value)
            box.setCurrentIndex(max(0, box.findData(option.default)))
            self.cups_form.addRow(option.label, box)
            self.option_boxes[option.key] = box

    def choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Ajouter des fichiers", str(Path.home()), documents.OPEN_FILTER)
        self.add_files(paths)

    def add_files(self, paths: list[str]) -> None:
        for path in paths:
            run_async(self, documents.Document, path, done=self._doc_loaded,
                      failed=lambda message, p=path: self.message.emit(f"{Path(p).name} : {message}", "error"))

    def _doc_loaded(self, document: documents.Document) -> None:
        self.docs.append(document)
        item = QListWidgetItem(document.name)
        item.setToolTip(str(document.source))
        self.list.addItem(item)
        self.list.setCurrentRow(self.list.count() - 1)

    def current_doc(self) -> documents.Document | None:
        row = self.list.currentRow()
        return self.docs[row] if 0 <= row < len(self.docs) else None

    def _doc_selected(self, _row: int) -> None:
        self.page_index = 0
        self.pages.clear()
        self._render()
        self._update_controls()

    def _remove_current(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            del self.docs[row]
            self.list.takeItem(row)
            self._render()
            self._update_controls()

    def _go(self, delta: int) -> None:
        document = self.current_doc()
        if document:
            self.page_index = min(document.page_count - 1, max(0, self.page_index + delta))
            self._render()
            self._update_controls()

    def _render(self) -> None:
        document = self.current_doc()
        self._render_token += 1
        token = self._render_token
        if not document:
            self.preview.set_image(None)
            return
        self.preview.clear_selection()

        def show(image):
            if token == self._render_token:
                self.preview.set_image(to_pixmap(image))

        def load(index):
            image = document.render(index, 110)
            image.thumbnail((1600, 1600))
            return image

        run_async(self, load, self.page_index, done=show,
                  failed=lambda message: self.message.emit(message, "error"))

    def _selection_changed(self) -> None:
        has_zone = self.preview.selection is not None
        self.zone_clear.setVisible(has_zone)
        self.zone_label.setText("Seule la zone sélectionnée sur cette page sera imprimée." if has_zone else
                                "Astuce : trace un rectangle sur la page pour n'imprimer que cette zone.")
        self.pages.setEnabled(not has_zone)

    def _update_controls(self) -> None:
        document = self.current_doc()
        self.print_button.setEnabled(document is not None)
        self.label_preview_button.setEnabled(document is not None)
        count = document.page_count if document else 0
        self.page_label.setText(f"Page {self.page_index + 1} sur {count}" if document else "")
        self.prev_button.setEnabled(document is not None and self.page_index > 0)
        self.next_button.setEnabled(document is not None and self.page_index < count - 1)

    def _zone(self) -> tuple[float, float, float, float] | None:
        zone = self.preview.selection
        return (zone.x(), zone.y(), zone.width(), zone.height()) if zone else None

    def _label_bitmaps(self, document, pages, zone, selected: brother.Label, threshold, dither):
        bitmaps = []
        for page in pages:
            image = document.render(page, 600 if zone else 300)
            if zone:
                image = documents.crop_normalized(image, zone)
            bitmaps.append(brother.to_bitmap(brother.fit_to_label(image, selected), threshold, dither))
        return bitmaps

    def _show_label_preview(self) -> None:
        document = self.current_doc()
        selected = brother.LABELS_BY_ID[self.label_box.currentData()]
        args = (document, [self.page_index], self._zone(), selected, self.threshold.value(), self.dither.isChecked())

        def show(bitmaps):
            dialog = QDialog(self)
            dialog.setWindowTitle("Aperçu de l'étiquette")
            layout = QVBoxLayout(dialog)
            view = QLabel()
            view.setPixmap(to_pixmap(bitmaps[0]).scaled(QSize(520, 700), Qt.AspectRatioMode.KeepAspectRatio,
                                                         Qt.TransformationMode.SmoothTransformation))
            layout.addWidget(view)
            layout.addWidget(label("Exactement ce qui sera imprimé, en noir et blanc. "
                                   "Vérifie que le code-barres est net.", "muted"))
            dialog.exec()

        run_async(self, self._label_bitmaps, *args, done=show, failed=lambda m: self.message.emit(m, "error"))

    def print_now(self) -> None:
        document, target = self.current_doc(), self.target()
        if not document or not target:
            return
        if not printing.valid_ranges(self.pages.text()):
            self.message.emit("Les pages doivent ressembler à 1-3, 5.", "error")
            return
        self.print_button.setEnabled(False)
        self.print_button.setText("Envoi…")
        if target.is_brother:
            self._print_label(document)
        else:
            self._print_cups(document, target)

    def _advanced_options(self) -> dict[str, str]:
        options = {}
        if self.number_up.currentData() != "1":
            options["number-up"] = self.number_up.currentData()
        if self.page_set.currentData():
            options["page-set"] = self.page_set.currentData()
        if self.scaling.value() != 100:
            options["scaling"] = str(self.scaling.value())
        if self.reverse.isChecked():
            options["outputorder"] = "reverse"
        if self.copies.value() > 1:
            options["Collate"] = "True" if self.collate.isChecked() else "False"
        if self.mirror.isChecked():
            options["mirror"] = "true"
        return options

    def _task(self, text: str):
        return self.toasts.task(text) if self.toasts else None

    def _finish_task(self, toast, text: str, kind: str = "ok") -> None:
        self.print_button.setText("Imprimer")
        self._update_controls()
        if toast:
            toast.finish(text, kind)
        else:
            self.message.emit(text, kind)

    def _track_job(self, job_text: str, name: str, toast) -> None:
        """Suit le travail dans CUPS jusqu'à sa vraie fin, pour dire ce qui s'est passé."""
        self.print_button.setText("Imprimer")
        self._update_controls()
        try:
            job_id = int(job_text.rsplit("-", 1)[-1])
        except ValueError:
            self._finish_task(toast, f"Envoyé à l'imprimante : {name}.", "ok")
            return
        timer = QTimer(self)
        timer.setInterval(1000)

        def poll():
            try:
                state = printing.job_state(job_id)
            except Exception as error:  # noqa: BLE001 - CUPS injoignable ou travail oublié
                timer.stop()
                self._finish_task(toast, f"Suivi de l'impression interrompu : {error}", "warn")
                return
            if state == 9:
                timer.stop()
                self._finish_task(toast, f"Impression terminée : {name}.", "ok")
            elif state == 7:
                timer.stop()
                self._finish_task(toast, f"Impression annulée : {name}.", "warn")
            elif state == 8:
                timer.stop()
                self._finish_task(toast, f"L'impression a échoué : {name}.", "error")
            elif toast:
                text = {3: "En attente de l'imprimante…", 4: "Travail suspendu.", 5: "Impression en cours…",
                        6: "Imprimante arrêtée : vérifie le papier et l'encre."}.get(state)
                if text:
                    toast.update_text(f"{text}\n{name}")

        timer.timeout.connect(poll)
        timer.start()
        poll()

    def _print_cups(self, document, target: Target) -> None:
        options = {key: box.currentData() for key, box in self.option_boxes.items()}
        options.update(self._advanced_options())
        zone = self._zone()
        page = self.page_index
        copies, ranges = self.copies.value(), self.pages.text()
        fit = self.fit.isChecked() and "scaling" not in options
        orientation = self.orientation.currentData()
        toast = self._task(f"Préparation de l'impression…\n{document.name}")

        def job():
            if zone:
                cropped = documents.crop_normalized(document.render(page, 300), zone)
                path = Path(tempfile.mkdtemp(prefix="imprimerie-")) / "zone.png"
                cropped.save(path, dpi=(300, 300))
                return printing.submit(printing.lp_args(target.id, str(path), document.name, copies, "",
                                                        options, True, orientation))
            return printing.submit(printing.lp_args(target.id, str(document.printable), document.name, copies, ranges,
                                                    options, fit, orientation))

        run_async(self, job, done=lambda job_id: self._track_job(job_id, document.name, toast),
                  failed=lambda text: self._finish_task(toast, text, "error"))

    def _print_label(self, document) -> None:
        selected = brother.LABELS_BY_ID[self.label_box.currentData()]
        zone = self._zone()
        host = self.cfg["brother_host"]
        pages = [self.page_index] if zone else printing.expand_ranges(self.pages.text(), document.page_count)
        copies, threshold, dither = self.copies.value(), self.threshold.value(), self.dither.isChecked()
        if not pages:
            self._finish_task(None, "Aucune page ne correspond à cette sélection.", "error")
            return
        total = len(pages) * copies
        if total > 3:
            answer = QMessageBox.question(self, "Imprimer", f"Cela utilisera {total} étiquettes. Continuer ?")
            if answer != QMessageBox.StandardButton.Yes:
                self._finish_task(None, "Impression annulée.", "warn")
                return

        def job():
            status = devices.brother_status(host)
            if not status.online:
                raise RuntimeError("La Brother est injoignable.")
            if status.detail and "snmpget" not in status.detail:
                raise RuntimeError(f"La Brother signale : {status.detail}")
            loaded = brother.label_from_media(status.media)
            if status.media and loaded is None:
                raise RuntimeError(f"Rouleau non reconnu : {status.media}")
            if loaded and loaded.id != selected.id:
                raise RuntimeError(f"Le rouleau chargé est « {loaded.name} », pas « {selected.name} ».")
            bitmaps = self._label_bitmaps(document, pages, zone, selected, threshold, dither) * copies
            brother.send(brother.build_job(bitmaps, selected), host)
            return len(bitmaps)

        toast = self._task(f"Préparation de {total} étiquette(s)…")
        run_async(self, job, done=lambda n: self._finish_task(toast, f"{n} étiquette(s) envoyée(s) à la Brother."),
                  failed=lambda text: self._finish_task(toast, text, "error"))


class JobsPage(QWidget):
    message = Signal(str, str)

    def __init__(self, cfg: dict):
        super().__init__()
        self.setObjectName("page")
        self.cfg = cfg
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(label("Travaux", "h1"))
        titles.addWidget(label("Les impressions de l'Epson. Les étiquettes de la Brother partent en direct : elles n'apparaissent pas ici.", "muted"))
        head.addLayout(titles, 1)
        self.cancel_button = QPushButton("Annuler le travail")
        self.cancel_button.clicked.connect(self._cancel)
        self.clear_button = QPushButton("Effacer la liste")
        icons.set_icon(self.clear_button, "trash")
        self.clear_button.setToolTip("Masque les travaux terminés. Ceux en cours restent affichés.")
        self.clear_button.clicked.connect(self._clear)
        head.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["N°", "Document", "Imprimante", "Date", "État"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.table, 1)
        self.empty = label("Aucun travail à afficher.", "muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.empty)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.rows: list[dict] = []
        self.all_rows: list[dict] = []
        self._update_buttons()

    def refresh(self) -> None:
        run_async(self, printing.list_jobs, done=self._fill, failed=lambda m: self.message.emit(m, "error"))

    def _fill(self, rows: list[dict]) -> None:
        selected = self.table.currentRow()
        self.all_rows = rows
        hidden = self.cfg.get("jobs_hidden_before", 0)
        self.rows = [job for job in rows if job["active"] or job["id"] > hidden]
        self.table.setRowCount(len(self.rows))
        colors = {"Terminé": "muted", "Annulé": "muted", "Échec": "error", "Arrêté": "error"}
        for index, job in enumerate(self.rows):
            when = dt.datetime.fromtimestamp(job["time"]).strftime("%d/%m/%Y %H:%M") if job["time"] else ""
            for column, value in enumerate([str(job["id"]), job["name"], job["printer"].replace("_", " "), when, job["state"]]):
                item = QTableWidgetItem(value)
                if column == 4 and job["state"] in colors:
                    item.setForeground(QColor(theme.color(colors[job["state"]])))
                self.table.setItem(index, column, item)
        if 0 <= selected < len(self.rows):
            self.table.selectRow(selected)
        self.empty.setVisible(not self.rows)
        self.table.setVisible(bool(self.rows))
        self._update_buttons()

    def _update_buttons(self) -> None:
        row = self.table.currentRow()
        self.cancel_button.setEnabled(0 <= row < len(self.rows) and self.rows[row]["active"])
        self.clear_button.setEnabled(any(not job["active"] for job in self.rows))

    def _clear(self) -> None:
        finished = [job["id"] for job in self.all_rows if not job["active"]]
        if finished:
            self.cfg["jobs_hidden_before"] = max(max(finished), self.cfg.get("jobs_hidden_before", 0))
            config.save(self.cfg)
            self._fill(self.all_rows)

    def _cancel(self) -> None:
        row = self.table.currentRow()
        if not 0 <= row < len(self.rows):
            return
        job = self.rows[row]
        run_async(self, printing.cancel_job, job["id"],
                  done=lambda _r: (self.message.emit(f"Travail {job['id']} annulé.", "ok"), self.refresh()),
                  failed=lambda m: self.message.emit(m, "error"))


class DevicesPage(QWidget):
    message = Signal(str, str)
    hostsChanged = Signal()
    themeChanged = Signal()
    testPageRequested = Signal()

    def __init__(self, cfg: dict):
        super().__init__()
        self.setObjectName("page")
        self.cfg = cfg
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(14)
        head = QHBoxLayout()
        head.addWidget(label("Appareils et réglages", "h1"), 1)
        self.search = QPushButton("Rechercher sur le réseau")
        self.search.clicked.connect(self.discover)
        head.addWidget(self.search)
        root.addLayout(head)

        epson, epson_layout = panel()
        epson_layout.addWidget(label("Epson XP-6000 — impression et scan", "title"))
        self.epson_state = label("", "muted")
        epson_layout.addWidget(self.epson_state)
        self.ink_grid = QGridLayout()
        self.ink_grid.setColumnStretch(1, 1)
        epson_layout.addLayout(self.ink_grid)
        self.epson_host = QLineEdit(cfg["epson_host"])
        self.epson_host.editingFinished.connect(self._hosts_edited)
        epson_form = QFormLayout()
        epson_form.addRow("Adresse IP", self.epson_host)
        epson_layout.addLayout(epson_form)
        self.queue_hint = label("", "warn")
        self.queue_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.queue_hint.hide()
        epson_layout.addWidget(self.queue_hint)
        actions = QHBoxLayout()
        test_page = QPushButton("Imprimer une page de test")
        icons.set_icon(test_page, "print")
        test_page.clicked.connect(self._test_page)
        open_admin = QPushButton("Ouvrir la page de l'imprimante")
        open_admin.setToolTip("Niveaux détaillés, nettoyage des buses, réseau et mise à jour du firmware")
        open_admin.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(f"http://{self.cfg['epson_host']}/")))
        actions.addWidget(test_page)
        actions.addWidget(open_admin)
        actions.addStretch(1)
        epson_layout.addLayout(actions)
        root.addWidget(epson)

        brother_panel, brother_layout = panel()
        brother_layout.addWidget(label("Brother QL-1060N — étiquettes", "title"))
        self.brother_state = label("", "muted")
        brother_layout.addWidget(self.brother_state)
        self.brother_host = QLineEdit(cfg["brother_host"])
        self.brother_host.editingFinished.connect(self._hosts_edited)
        brother_form = QFormLayout()
        brother_form.addRow("Adresse IP", self.brother_host)
        brother_layout.addLayout(brother_form)
        brother_layout.addWidget(label("Elle n'a pas de scanner.", "muted"))
        root.addWidget(brother_panel)

        settings_panel, settings_layout = panel()
        settings_layout.addWidget(label("Réglages", "title"))
        settings_form = QFormLayout()
        self.theme_box = compact_combo()
        self.theme_box.addItem("Sombre", "sombre")
        self.theme_box.addItem("Clair", "clair")
        self.theme_box.setCurrentIndex(max(0, self.theme_box.findData(cfg.get("theme", "sombre"))))
        self.theme_box.activated.connect(self._theme_chosen)
        settings_form.addRow("Apparence", self.theme_box)
        folder_row = QHBoxLayout()
        self.scan_dir = label(cfg["scan_dir"], "muted")
        choose = QPushButton("Changer")
        icons.set_icon(choose, "folder")
        choose.clicked.connect(self._choose_folder)
        folder_row.addWidget(self.scan_dir, 1)
        folder_row.addWidget(choose)
        settings_form.addRow("Dossier d'export", folder_row)
        settings_layout.addLayout(settings_form)
        root.addWidget(settings_panel)
        root.addStretch(1)

    def _theme_chosen(self) -> None:
        self.cfg["theme"] = self.theme_box.currentData()
        config.save(self.cfg)
        self.themeChanged.emit()

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Dossier d'export", self.cfg["scan_dir"])
        if path:
            self.cfg["scan_dir"] = path
            config.save(self.cfg)
            self.scan_dir.setText(path)

    def _test_page(self) -> None:
        answer = QMessageBox.question(self, "Page de test", "Imprimer une page de test sur l'Epson ? Elle utilisera une feuille et de l'encre.")
        if answer == QMessageBox.StandardButton.Yes:
            self.testPageRequested.emit()

    def _set(self, widget: QLabel, text: str, kind: str) -> None:
        widget.setObjectName(kind)
        widget.setText(text)
        widget.style().polish(widget)

    def _hosts_edited(self) -> None:
        epson, label_printer = self.epson_host.text().strip(), self.brother_host.text().strip()
        if (epson, label_printer) != (self.cfg["epson_host"], self.cfg["brother_host"]):
            self.cfg["epson_host"], self.cfg["brother_host"] = epson, label_printer
            config.save(self.cfg)
            self.hostsChanged.emit()
            self.refresh()

    def refresh(self) -> None:
        run_async(self, devices.ipp_status, self.cfg["epson_host"], done=self._epson, failed=lambda m: self._set(self.epson_state, m, "error"))
        run_async(self, devices.brother_status, self.cfg["brother_host"], done=self._brother, failed=lambda m: self._set(self.brother_state, m, "error"))
        run_async(self, printing.list_queues, done=self._check_queue, failed=lambda _m: None)

    def _check_queue(self, queues: dict) -> None:
        wanted = self.cfg["epson_host"]
        for name, info in queues.items():
            uri = info.get("device-uri", "")
            if "EPSON" in name.upper() and wanted not in uri:
                command = f"sudo lpadmin -p {name} -E -v ipp://{wanted}/ipp/print -m everywhere"
                self.queue_hint.setText(f"La file d'impression pointe encore vers {uri}. Pour la corriger :\n{command}")
                self.queue_hint.show()
                return
        self.queue_hint.hide()

    def _epson(self, status: devices.Status) -> None:
        if status.online:
            detail = f" — {status.detail}" if status.detail else ""
            self._set(self.epson_state, f"{status.state}{detail}", "warn" if status.detail else "ok")
        else:
            self._set(self.epson_state, "Injoignable. Elle est peut-être éteinte, ou a changé d'adresse.", "error")
        while self.ink_grid.count():
            item = self.ink_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for row, (name, color, level) in enumerate(status.ink):
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(max(0, level))
            bar.setTextVisible(False)
            chunk = theme.color("warn") if level <= 15 else theme.color("muted")
            bar.setStyleSheet(f"QProgressBar::chunk {{ background: {chunk}; border-radius: 3px; }}")
            self.ink_grid.addWidget(QLabel(name), row, 0)
            self.ink_grid.addWidget(bar, row, 1)
            self.ink_grid.addWidget(QLabel(f"{level} %"), row, 2)

    def _brother(self, status: devices.Status) -> None:
        if not status.online:
            self._set(self.brother_state, "Injoignable. Elle est peut-être éteinte, ou a changé d'adresse.", "error")
            return
        media = f" — rouleau {status.media.split(' /')[0]}" if status.media else ""
        detail = f" — {status.detail}" if status.detail else ""
        self._set(self.brother_state, f"{status.state}{media}{detail}", "warn" if status.detail else "ok")

    def discover(self) -> None:
        self.search.setEnabled(False)
        self.search.setText("Recherche…")

        def finish(found: dict) -> None:
            self.search.setEnabled(True)
            self.search.setText("Rechercher sur le réseau")
            if "epson" in found:
                self.epson_host.setText(found["epson"])
            if "brother" in found:
                self.brother_host.setText(found["brother"])
            self._hosts_edited()
            missing = [name for key, name in (("epson", "Epson"), ("brother", "Brother")) if key not in found]
            if missing:
                self.message.emit("Introuvable sur le réseau : " + ", ".join(missing) + ".", "warn")
            else:
                self.message.emit(f"Trouvées : Epson {found['epson']}, Brother {found['brother']}.", "ok")

        def failed(text: str) -> None:
            self.search.setEnabled(True)
            self.search.setText("Rechercher sur le réseau")
            self.message.emit(text, "error")

        run_async(self, devices.discover, done=finish, failed=failed)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Imprimerie")
        self.resize(1360, 860)
        self.setMinimumSize(1160, 700)
        self.setAcceptDrops(True)
        self.cfg = config.load()
        theme.set_theme(self.cfg.get("theme", "sombre"))

        central = QWidget()
        central.setObjectName("page")
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        nav = QFrame()
        nav.setObjectName("nav")
        nav.setFixedWidth(160)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(8, 14, 8, 12)
        nav_layout.setSpacing(2)
        brand = label("Imprimerie", "brand")
        brand.setContentsMargins(10, 0, 0, 0)
        nav_layout.addWidget(brand)
        nav_layout.addSpacing(10)

        self.stack = QStackedWidget()
        self.print_page = PrintPage(self.cfg)
        self.scan_page = EditorPage(self.cfg)
        self.jobs_page = JobsPage(self.cfg)
        self.devices_page = DevicesPage(self.cfg)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, (name, icon_name, page) in enumerate([("Imprimer", "print", self.print_page),
                                                         ("Scanner et PDF", "scan", self.scan_page),
                                                         ("Travaux", "jobs", self.jobs_page),
                                                         ("Appareils", "devices", self.devices_page)]):
            self.stack.addWidget(page)
            button = QPushButton(name)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            icons.set_icon(button, icon_name, 18, "muted")
            button.clicked.connect(lambda _c=False, i=index: self.show_page(i))
            self.group.addButton(button, index)
            nav_layout.addWidget(button)
        nav_layout.addStretch(1)
        self.status_epson = self._status_row(nav_layout, "Epson XP-6000")
        self.status_brother = self._status_row(nav_layout, "Brother QL-1060N")
        outer.addWidget(nav)
        outer.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.toasts = ToastHost(central)
        for page in (self.print_page, self.scan_page, self.jobs_page, self.devices_page):
            page.message.connect(self.notify)
        self.print_page.toasts = self.toasts
        self.scan_page.toasts = self.toasts
        self.scan_page.sendToPrint.connect(self._scan_to_print)
        self.devices_page.hostsChanged.connect(self._hosts_changed)
        self.devices_page.themeChanged.connect(self.apply_theme)
        self.devices_page.testPageRequested.connect(self._print_test_page)

        self.poll = QTimer(self)
        self.poll.setInterval(3000)
        self.poll.timeout.connect(self._poll)
        self.poll.start()
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(30000)
        self.status_timer.timeout.connect(self.refresh_status)
        self.status_timer.start()
        self.refresh_status()

        open_action = QAction(self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._open_files)
        self.addAction(open_action)

    def _status_row(self, layout: QVBoxLayout, name: str) -> tuple[QLabel, QLabel]:
        row = QHBoxLayout()
        row.setSpacing(8)
        dot = QLabel()
        dot.setFixedSize(9, 9)
        dot.setToolTip("Vérification…")
        text = label(name, "small")
        row.addWidget(dot)
        row.addWidget(text, 1)
        layout.addLayout(row)
        self._paint_dot(dot, "faint")
        return dot, text

    @staticmethod
    def _paint_dot(dot: QLabel, token: str) -> None:
        dot.setStyleSheet(f"background: {theme.color(token)}; border-radius: 4px;")

    def _show_status(self, row, status: devices.Status) -> None:
        dot, _text = row
        token = "error" if not status.online else "warn" if status.detail else "faint"
        self._paint_dot(dot, token)
        dot.setToolTip(status.state + (f" : {status.detail}" if status.detail else ""))

    def refresh_status(self) -> None:
        run_async(self, devices.ipp_status, self.cfg["epson_host"], done=lambda s: self._show_status(self.status_epson, s))
        run_async(self, devices.brother_status, self.cfg["brother_host"], done=lambda s: self._show_status(self.status_brother, s))

    def apply_theme(self) -> None:
        theme.set_theme(self.cfg.get("theme", "sombre"))
        QApplication.instance().setStyleSheet(theme.build_qss())
        icons.refresh_all()
        self.scan_page._show_current(keep_view=True)
        self.refresh_status()
        self.update()

    def _open_files(self) -> None:
        if self.stack.currentWidget() is self.scan_page:
            self.scan_page.choose_files()
        else:
            self.print_page.choose_files()

    def show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.group.button(index).setChecked(True)
        if index == 1:
            self.scan_page.refresh()
        elif index == 3:
            self.devices_page.refresh()
        self._poll()

    def _poll(self) -> None:
        if self.stack.currentWidget() is self.jobs_page:
            self.jobs_page.refresh()

    def _hosts_changed(self) -> None:
        self.print_page.refresh_printers()
        self.scan_page.refresh()
        self.refresh_status()

    def _scan_to_print(self, path: str) -> None:
        self.print_page.add_files([path])
        self.show_page(0)

    def _print_test_page(self) -> None:
        queues = [t for t in self.print_page.targets if "EPSON" in t.id.upper()]
        if not queues:
            self.notify("Aucune file d'impression Epson n'est configurée.", "error")
            return
        toast = self.toasts.task("Page de test en préparation…")
        args = printing.lp_args(queues[0].id, "/usr/share/cups/data/testprint", "Page de test")
        run_async(self, printing.submit, args,
                  done=lambda job: self.print_page._track_job(job, "page de test", toast),
                  failed=lambda text: toast.finish(text, "error"))

    def notify(self, text: str, kind: str = "ok") -> None:
        self.toasts.show_message(text, kind)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if self.stack.currentWidget() is self.scan_page:
            self.scan_page.add_files(paths)
            return
        self.print_page.add_files(paths)
        self.show_page(0)

    def closeEvent(self, event):
        # Les pages de l'éditeur sont gardées : elles reviendront au prochain lancement.
        self.scan_page.flush_session()
        event.accept()


def run(argv: list[str]) -> int:
    app = QApplication(argv)
    app.setApplicationName("Imprimerie")
    app.setDesktopFileName("imprimerie")
    app.setStyle("Fusion")
    theme.set_theme(config.load().get("theme", "sombre"))
    app.setStyleSheet(theme.build_qss())
    window = MainWindow()
    window.show()
    files = [arg for arg in argv[1:] if not arg.startswith("-")]
    if files:
        window.print_page.add_files(files)
    return app.exec()
