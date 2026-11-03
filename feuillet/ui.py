"""Interface Qt : Imprimer, Scanner, Travaux, Appareils."""
from __future__ import annotations

import datetime as dt
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QBoxLayout, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLayout, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import brother, config, devices, discovery, documents, icons, printing, theme
from .editor_ui import EditorPage
from .setup_ui import AddDeviceDialog, admin_task
from .widgets import (
    NARROW, WIDE, MEDIUM, Section, ToastHost, ViewSwitch, compact_combo, empty_state, label, layout_mode, panel, run_async,
    to_pixmap,
)

class Preview(QWidget):
    """Affiche une page. Un glisser à la souris y sélectionne une zone, si la sélection est active."""
    selectionChanged = Signal()

    def __init__(self, hint: str = "", selectable: bool = False):
        super().__init__()
        self.setObjectName("preview")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumSize(200, 200)
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
    uri: str = ""


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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)
        self.view_switch = ViewSwitch("Aperçu")
        self.view_switch.changed.connect(lambda _shown: self._apply_view())
        self.view_switch.hide()
        outer.addWidget(self.view_switch)
        root = QHBoxLayout()
        root.setSpacing(8)
        outer.addLayout(root, 1)

        files, files_layout = panel()
        self.files_panel = files
        files_layout.addWidget(label("Documents", "title"))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._doc_selected)
        files_layout.addWidget(self.list, 1)
        self.add_button = QPushButton("Ajouter des fichiers…")
        self.add_button.clicked.connect(self.choose_files)
        self.remove_button = QPushButton("Retirer de la liste")
        self.remove_button.clicked.connect(self._remove_current)
        files_layout.addWidget(self.add_button)
        files_layout.addWidget(self.remove_button)
        root.addWidget(files)

        self.center_widget = QWidget()
        center = QVBoxLayout(self.center_widget)
        center.setContentsMargins(0, 0, 0, 0)
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
        root.addWidget(self.center_widget, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.side_scroll = scroll
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
        root.addWidget(scroll, 1)
        self.layout_mode = WIDE
        self.set_layout_mode(WIDE)

        self.refresh_printers()
        self._update_controls()

    def set_layout_mode(self, mode: str) -> None:
        self.layout_mode = mode
        files, side = {WIDE: (210, 300), MEDIUM: (150, 270), NARROW: (110, 0)}[mode]
        self.files_panel.setFixedWidth(files)
        if side:
            self.side_scroll.setFixedWidth(side)
        else:
            self.side_scroll.setMinimumWidth(0)
            self.side_scroll.setMaximumWidth(16777215)
        wide = mode == WIDE
        self.add_button.setText("Ajouter des fichiers…" if wide else "Ajouter…")
        self.remove_button.setText("Retirer de la liste" if wide else "Retirer")
        self.prev_button.setText("‹ Page précédente" if wide else "‹")
        self.next_button.setText("Page suivante ›" if wide else "›")
        self.view_switch.setVisible(mode == NARROW)
        self._apply_view()

    def _apply_view(self) -> None:
        narrow = self.layout_mode == NARROW
        settings = narrow and self.view_switch.showing_settings()
        self.center_widget.setVisible(not settings)
        self.files_panel.setVisible(not settings)
        self.side_scroll.setVisible(not narrow or settings)

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
                self.targets.append(Target(name, title.replace("_", " "), uri=info.get("device-uri", "")))
        if self.cfg.get("brother_host"):
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
        self._update_controls()

    def target(self) -> Target | None:
        index = self.printer.currentIndex()
        return self.targets[index] if 0 <= index < len(self.targets) else None

    def _printer_changed(self) -> None:
        target = self.target()
        if not target:
            self._set_state("Aucune imprimante. Ajoute-en une dans Réglages.", "warn")
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
            uri = target.uri
            run_async(self, devices.queue_status, uri,
                      done=lambda status: self._queue_ready(status, uri), failed=self._state_failed)

    def _set_state(self, text: str, kind: str) -> None:
        self.printer_state.setObjectName(kind)
        self.printer_state.setText(text)
        self.printer_state.style().polish(self.printer_state)

    def _state_failed(self, message: str) -> None:
        self._set_state(message, "error")

    def _queue_ready(self, status: devices.Status, uri: str) -> None:
        target = self.target()
        if not target or target.uri != uri:
            return
        if not status.online:
            self._set_state("Injoignable. Va dans « Réglages » pour la rechercher.", "error")
        elif status.detail:
            self._set_state(f"{status.state} : {status.detail}", "warn")
        else:
            self._set_state(status.state, "ok")

    def _brother_ready(self, status: devices.Status) -> None:
        target = self.target()
        if not target or not target.is_brother:
            return
        self.media_text = status.media
        detected = brother.label_from_media(status.media)
        if detected:
            self.label_box.setCurrentIndex(self.label_box.findData(detected.id))
        if not status.online:
            self._set_state("Injoignable. Va dans « Réglages » pour la rechercher.", "error")
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
        self.print_button.setEnabled(document is not None and self.target() is not None)
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
                path = Path(tempfile.mkdtemp(prefix="feuillet-")) / "zone.png"
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
        self.head = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        head = self.head
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
        buttons = QHBoxLayout()
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.clear_button)
        head.addLayout(buttons)
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

    def set_layout_mode(self, mode: str) -> None:
        narrow = mode == NARROW
        self.head.setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)
        self.table.setColumnHidden(2, mode != WIDE)     # l'imprimante, souvent la même
        self.table.setColumnHidden(0, narrow)

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


def setting_row(title: str, description: str, control=None) -> QWidget:
    """Une ligne de réglage : son intitulé à gauche, son action à droite."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(14, 10, 14, 10)
    layout.setSpacing(16)
    texts = QVBoxLayout()
    texts.setSpacing(1)
    texts.addWidget(label(title))
    if description:
        texts.addWidget(label(description, "small"))
    layout.addLayout(texts, 1)
    if control is not None:
        if isinstance(control, QWidget):
            layout.addWidget(control)
        else:
            layout.addLayout(control)
    return row


def settings_group(*rows: QWidget) -> QFrame:
    """Des lignes regroupées dans un même bloc, séparées par un filet."""
    group = QFrame()
    group.setObjectName("group")
    layout = QVBoxLayout(group)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    for index, row in enumerate(rows):
        if index:
            line = QFrame()
            line.setObjectName("hairline")
            line.setFixedHeight(1)
            layout.addWidget(line)
        layout.addWidget(row)
    return group


def state_control() -> tuple[QHBoxLayout, QLabel, QLabel]:
    """Un voyant et son texte, pour l'état d'un appareil."""
    row = QHBoxLayout()
    row.setSpacing(7)
    dot = QLabel()
    dot.setFixedSize(8, 8)
    text = label("Vérification…", "muted")
    text.setWordWrap(False)
    row.addWidget(dot)
    row.addWidget(text)
    return row, dot, text


class DevicesPage(QWidget):
    message = Signal(str, str)
    devicesChanged = Signal()
    themeChanged = Signal()
    testPageRequested = Signal(str)

    def __init__(self, cfg: dict):
        super().__init__()
        self.setObjectName("page")
        self.cfg = cfg
        self.queues: dict = {}
        self.default_queue = ""
        self._signature = None
        self.cells: dict = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("page")
        scroll.setWidget(content)
        centered = QHBoxLayout(content)
        centered.setContentsMargins(20, 20, 20, 24)
        column = QWidget()
        column.setMaximumWidth(680)
        centered.addStretch(1)
        centered.addWidget(column, 100)
        centered.addStretch(1)
        root = QVBoxLayout(column)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.addWidget(label("Réglages", "h1"), 1)
        self.add_button = QPushButton("Ajouter un appareil")
        self.add_button.setObjectName("primary")
        self.add_button.clicked.connect(self.open_wizard)
        head.addWidget(self.add_button)
        root.addLayout(head)

        self.service_box = QWidget()
        service_layout = QVBoxLayout(self.service_box)
        service_layout.setContentsMargins(0, 8, 0, 0)
        self.start_service_button = QPushButton("Activer")
        self.start_service_button.clicked.connect(self._start_service)
        service_layout.addWidget(settings_group(setting_row(
            "Le service d'impression est arrêté", "Il faut l'activer une fois pour pouvoir imprimer", self.start_service_button)))
        self.service_box.hide()
        root.addWidget(self.service_box)

        self.devices_box = QVBoxLayout()
        self.devices_box.setSpacing(8)
        root.addLayout(self.devices_box)

        root.addSpacing(10)
        root.addWidget(label("Application", "sectionTitle"))
        self.theme_box = compact_combo()
        self.theme_box.addItem("Sombre", "sombre")
        self.theme_box.addItem("Clair", "clair")
        self.theme_box.setFixedWidth(140)
        self.theme_box.setCurrentIndex(max(0, self.theme_box.findData(cfg.get("theme", "sombre"))))
        self.theme_box.activated.connect(self._theme_chosen)
        choose = QPushButton("Changer")
        choose.clicked.connect(self._choose_folder)
        self.scan_dir_row = setting_row("Dossier d'export", cfg["scan_dir"], choose)
        self.scan_dir = self.scan_dir_row.findChildren(QLabel)[1]
        root.addWidget(settings_group(
            setting_row("Apparence", "", self.theme_box),
            self.scan_dir_row,
        ))
        root.addStretch(1)

    def set_layout_mode(self, mode: str) -> None:
        pass    # les lignes se replient seules, et la page défile

    # --- Liste des appareils --------------------------------------------------------------

    def _clear_devices(self) -> None:
        while self.devices_box.count():
            item = self.devices_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.cells = {}

    def refresh(self) -> None:
        def load():
            running = printing.service_running()
            return running, (printing.list_queues() if running else {}), (printing.default_queue() if running else None)

        run_async(self, load, done=self._loaded, failed=lambda text: self.message.emit(text, "error"))

    def _loaded(self, result) -> None:
        running, queues, default = result
        self.service_box.setVisible(not running)
        self.queues = {name: info for name, info in queues.items() if not printing.is_raw_brother_queue(name, info)}
        self.default_queue = default or ""
        signature = (tuple(sorted((name, info.get("device-uri", "")) for name, info in self.queues.items())),
                     self.default_queue, self.cfg.get("brother_host", ""),
                     tuple(scanner["id"] for scanner in self.cfg["scanners"]))
        if signature != self._signature:
            self._signature = signature
            self._rebuild()
        for name, info in self.queues.items():
            uri = info.get("device-uri", "")
            run_async(self, devices.queue_status, uri, done=lambda status, n=name: self._queue_status(n, status))
        if self.cfg.get("brother_host"):
            run_async(self, devices.brother_status, self.cfg["brother_host"], done=self._brother_status)

    def _rebuild(self) -> None:
        self._clear_devices()
        if not self.queues and not self.cfg.get("brother_host") and not self.cfg["scanners"]:
            empty = settings_group(setting_row("Aucun appareil pour l'instant",
                                               "Clique sur « Ajouter un appareil » : l'application cherche et configure tout."))
            self.devices_box.addSpacing(10)
            self.devices_box.addWidget(empty)
            return
        if self.queues or self.cfg.get("brother_host"):
            self.devices_box.addSpacing(10)
            self.devices_box.addWidget(label("Imprimantes", "sectionTitle"))
        for name, info in self.queues.items():
            self.devices_box.addWidget(self._queue_group(name, info))
        if self.cfg.get("brother_host"):
            self.devices_box.addWidget(self._brother_group())
        if self.cfg["scanners"]:
            self.devices_box.addSpacing(10)
            self.devices_box.addWidget(label("Scanners", "sectionTitle"))
            rows = []
            for scanner in self.cfg["scanners"]:
                remove = QPushButton("Retirer")
                remove.clicked.connect(lambda _c=False, s=scanner: self._remove_scanner(s))
                where = scanner.get("host") or "USB"
                rows.append(setting_row(scanner["name"], where, remove))
            self.devices_box.addWidget(settings_group(*rows))

    def _queue_group(self, name: str, info: dict) -> QFrame:
        title = (info.get("printer-info") or name).replace("_", " ")
        uri = info.get("device-uri", "")
        state, dot, text = state_control()
        ink = QWidget()
        grid = QGridLayout(ink)
        grid.setContentsMargins(14, 10, 14, 12)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(5)
        ink.hide()
        self.cells[name] = {"dot": dot, "text": text, "ink": ink, "grid": grid, "bars": []}
        where = discovery.host_of_uri(uri) or "USB"
        description = f"{where}  ·  par défaut" if name == self.default_queue else where
        rows = [setting_row(title, description, state), ink]

        actions = QHBoxLayout()
        actions.setSpacing(6)
        if name != self.default_queue:
            default = QPushButton("Par défaut")
            default.clicked.connect(lambda: self._set_default(name))
            actions.addWidget(default)
        test = QPushButton("Page de test")
        test.clicked.connect(lambda: self._test_page(name))
        actions.addWidget(test)
        if uri.startswith(("ipp://", "ipps://")):
            page = QPushButton("Interface")
            page.setToolTip("Nettoyage des buses, réseau, firmware")
            page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(f"http://{discovery.host_of_uri(uri)}/")))
            actions.addWidget(page)
        if info.get("printer-state") == 5:
            resume = QPushButton("Reprendre")
            resume.setToolTip("Une erreur a mis cette imprimante en pause")
            resume.clicked.connect(lambda: self._admin_action(lambda password: printing.resume_queue(name, password), f"{title} a repris."))
            actions.addWidget(resume)
        remove = QPushButton("Retirer")
        remove.clicked.connect(lambda: self._remove_queue(name, title))
        actions.addWidget(remove)
        actions_row = QWidget()
        actions_layout = QHBoxLayout(actions_row)
        actions_layout.setContentsMargins(14, 8, 14, 10)
        actions_layout.addStretch(1)
        actions_layout.addLayout(actions)
        rows.append(actions_row)
        return settings_group(*rows)

    def _brother_group(self) -> QFrame:
        state, dot, text = state_control()
        self.cells["__brother__"] = {"dot": dot, "text": text}
        self.media_label = label("—", "muted")
        self.media_label.setWordWrap(False)
        remove = QPushButton("Retirer")
        remove.clicked.connect(self._remove_brother)
        return settings_group(
            setting_row("Brother QL-1060N", f"{self.cfg['brother_host']}  ·  étiquettes", state),
            setting_row("Rouleau chargé", "", self.media_label),
            setting_row("Retirer cette imprimante", "", remove),
        )

    @staticmethod
    def _show_state(dot: QLabel, text: QLabel, value: str, token: str) -> None:
        dot.setStyleSheet(f"background: {theme.color(token)}; border-radius: 4px;")
        text.setText(value)

    def _queue_status(self, name: str, status: devices.Status) -> None:
        cell = self.cells.get(name)
        if not cell:
            return
        if status.online:
            detail = f" : {status.detail}" if status.detail else ""
            self._show_state(cell["dot"], cell["text"], f"{status.state}{detail}", "warn" if status.detail else "ok")
        else:
            self._show_state(cell["dot"], cell["text"], "Injoignable", "error")
        cell["ink"].setVisible(bool(status.ink))
        # Les barres sont réutilisées d'une actualisation à l'autre : rien ne clignote.
        if len(cell["bars"]) != len(status.ink):
            for index in range(len(status.ink)):
                cell["grid"].setColumnStretch(index, 1)
                title = label("", "small")
                title.setWordWrap(False)
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setTextVisible(False)
                amount = QLabel()
                cell["grid"].addWidget(title, 0, index)
                cell["grid"].addWidget(bar, 1, index)
                cell["grid"].addWidget(amount, 2, index)
                cell["bars"].append((title, bar, amount))
        for (title, bar, amount), (ink_name, _color, level) in zip(cell["bars"], status.ink):
            title.setText(ink_name)
            bar.setValue(max(0, level))
            chunk = theme.color("warn") if 0 <= level <= 15 else theme.color("muted")
            bar.setStyleSheet(f"QProgressBar::chunk {{ background: {chunk}; }}")
            amount.setText(f"{level} %" if level >= 0 else "?")

    def _brother_status(self, status: devices.Status) -> None:
        cell = self.cells.get("__brother__")
        if not cell:
            return
        if not status.online:
            self._show_state(cell["dot"], cell["text"], "Injoignable", "error")
            self.media_label.setText("—")
            return
        detail = f" : {status.detail}" if status.detail else ""
        self._show_state(cell["dot"], cell["text"], f"{status.state}{detail}", "warn" if status.detail else "ok")
        self.media_label.setText(status.media.split(" /")[0].replace("mm x ", " × ").replace("mm", " mm") if status.media else "Inconnu")

    # --- Actions --------------------------------------------------------------------------

    def open_wizard(self) -> None:
        dialog = AddDeviceDialog(self, self.cfg)
        dialog.exec()
        if dialog.changed:
            self._signature = None
            self.refresh()
            self.devicesChanged.emit()

    def _admin_action(self, job, success: str) -> None:
        def done(_result):
            self.message.emit(success, "ok")
            self._signature = None
            self.refresh()
            self.devicesChanged.emit()

        admin_task(self, job, done, lambda text: self.message.emit(text, "error"))

    def _start_service(self) -> None:
        self._admin_action(printing.start_service, "Le service d'impression est activé.")

    def _set_default(self, name: str) -> None:
        def done(_result):
            self._signature = None
            self.refresh()
            self.devicesChanged.emit()

        run_async(self, printing.set_default_queue, name, done=done, failed=lambda text: self.message.emit(text, "error"))

    def _remove_queue(self, name: str, title: str) -> None:
        answer = QMessageBox.question(self, "Retirer", f"Retirer {title} de cet ordinateur ?")
        if answer == QMessageBox.StandardButton.Yes:
            self._admin_action(lambda password: printing.remove_queue(name, password), f"{title} est retirée.")

    def _remove_brother(self) -> None:
        answer = QMessageBox.question(self, "Retirer", "Retirer la Brother QL-1060N de cet ordinateur ?")
        if answer == QMessageBox.StandardButton.Yes:
            self.cfg["brother_host"] = ""
            config.save(self.cfg)
            self._signature = None
            self.refresh()
            self.devicesChanged.emit()

    def _remove_scanner(self, scanner: dict) -> None:
        answer = QMessageBox.question(self, "Retirer", f"Retirer le scanner {scanner['name']} ?")
        if answer == QMessageBox.StandardButton.Yes:
            self.cfg["scanners"] = [item for item in self.cfg["scanners"] if item["id"] != scanner["id"]]
            if self.cfg.get("scanner") == scanner["id"]:
                self.cfg["scanner"] = self.cfg["scanners"][0]["id"] if self.cfg["scanners"] else ""
            config.save(self.cfg)
            self._signature = None
            self.refresh()
            self.devicesChanged.emit()

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

    def _test_page(self, name: str) -> None:
        answer = QMessageBox.question(self, "Page de test", "Imprimer une page de test ? Elle utilisera une feuille et de l'encre.")
        if answer == QMessageBox.StandardButton.Yes:
            self.testPageRequested.emit(name)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Feuillet")
        self.resize(1360, 860)
        # Pas de grand minimum : un gestionnaire en mosaïque l'ignore de toute façon, et tout déborde.
        self.setMinimumSize(520, 400)
        self.setAcceptDrops(True)
        self.cfg = config.load()
        theme.set_theme(self.cfg.get("theme", "sombre"))

        central = QWidget()
        central.setObjectName("page")
        self.outer = QBoxLayout(QBoxLayout.Direction.LeftToRight, central)
        outer = self.outer
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        nav = QFrame()
        self.nav = nav
        nav.setObjectName("nav")
        self.nav_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, nav)
        nav_layout = self.nav_layout
        nav_layout.setContentsMargins(8, 14, 8, 12)
        nav_layout.setSpacing(2)
        self.brand = label("Feuillet", "brand")
        self.brand.setContentsMargins(10, 0, 0, 10)
        nav_layout.addWidget(self.brand)

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
                                                         ("Réglages", "devices", self.devices_page)]):
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
        self.status_box = QWidget()
        self.status_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, self.status_box)
        self.status_layout.setContentsMargins(0, 0, 0, 0)
        self.status_layout.setSpacing(4)
        nav_layout.addWidget(self.status_box)
        self.status_rows: list = []
        outer.addWidget(nav)
        outer.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        # Le contenu ne doit pas bloquer la fenêtre à sa largeur : c'est elle qui choisit le mode d'affichage.
        self.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)

        self.toasts = ToastHost(central)
        for page in (self.print_page, self.scan_page, self.jobs_page, self.devices_page):
            page.message.connect(self.notify)
        self.print_page.toasts = self.toasts
        self.scan_page.toasts = self.toasts
        self.scan_page.sendToPrint.connect(self._scan_to_print)
        self.devices_page.devicesChanged.connect(self._devices_changed)
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
        self.layout_mode = ""
        self._apply_layout()
        self.refresh_status()
        self.devices_page.refresh()
        self._offer_setup()

        open_action = QAction(self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._open_files)
        self.addAction(open_action)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_layout()

    def _offer_setup(self) -> None:
        """Au premier lancement, sans appareil : on va droit à la page où en ajouter."""
        if not self.print_page.targets and not self.cfg["scanners"] and not self.cfg.get("setup_offered"):
            self.cfg["setup_offered"] = True
            config.save(self.cfg)
            self.show_page(3)

    def _apply_layout(self) -> None:
        mode = layout_mode(self.width())
        if mode == self.layout_mode:
            return
        self.layout_mode = mode
        wide = mode == WIDE
        # Dès que la place manque, la navigation passe en haut : elle ne prend plus de largeur.
        self.outer.setDirection(QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom)
        self.nav_layout.setDirection(QBoxLayout.Direction.TopToBottom if wide else QBoxLayout.Direction.LeftToRight)
        self.nav_layout.setContentsMargins(*((8, 14, 8, 12) if wide else (8, 6, 8, 6)))
        self.nav.setFixedWidth(160) if wide else self.nav.setMaximumWidth(16777215)
        if not wide:
            self.nav.setMinimumWidth(0)
        self.nav.setObjectName("nav" if wide else "navTop")
        self.nav.style().polish(self.nav)
        self.brand.setVisible(wide)
        self.status_layout.setDirection(QBoxLayout.Direction.TopToBottom if wide else QBoxLayout.Direction.LeftToRight)
        for _dot, text in self.status_rows:
            text.setVisible(wide)
        for page in (self.print_page, self.scan_page, self.jobs_page, self.devices_page):
            page.set_layout_mode(mode)

    def _status_row(self, name: str) -> tuple[QLabel, QLabel]:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        dot = QLabel()
        dot.setFixedSize(9, 9)
        dot.setToolTip(f"{name} : vérification…")
        dot.setProperty("device", name)
        text = label(name, "small")
        text.setVisible(self.layout_mode == WIDE)
        layout.addWidget(dot)
        layout.addWidget(text, 1)
        self.status_layout.addWidget(row)
        self._paint_dot(dot, "faint")
        return dot, text

    @staticmethod
    def _paint_dot(dot: QLabel, token: str) -> None:
        dot.setStyleSheet(f"background: {theme.color(token)}; border-radius: 4px;")

    def _show_status(self, row, status: devices.Status) -> None:
        dot, _text = row
        try:
            token = "error" if not status.online else "warn" if status.detail else "faint"
            self._paint_dot(dot, token)
            detail = f" : {status.detail}" if status.detail else ""
            dot.setToolTip(f"{dot.property('device')} — {status.state}{detail}")
        except RuntimeError:
            pass    # la ligne a été remplacée pendant la vérification

    def refresh_status(self) -> None:
        run_async(self, printing.list_queues, done=self._status_queues, failed=lambda _text: self._status_queues({}))

    def _status_queues(self, queues: dict) -> None:
        while self.status_layout.count():
            item = self.status_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.status_rows = []
        for name, info in queues.items():
            if printing.is_raw_brother_queue(name, info):
                continue
            row = self._status_row((info.get("printer-info") or name).replace("_", " "))
            self.status_rows.append(row)
            run_async(self, devices.queue_status, info.get("device-uri", ""), done=lambda s, r=row: self._show_status(r, s))
        if self.cfg.get("brother_host"):
            row = self._status_row("Brother QL-1060N")
            self.status_rows.append(row)
            run_async(self, devices.brother_status, self.cfg["brother_host"], done=lambda s, r=row: self._show_status(r, s))

    def apply_theme(self) -> None:
        theme.set_theme(self.cfg.get("theme", "sombre"))
        QApplication.instance().setStyleSheet(theme.build_qss())
        icons.refresh_all()
        self.scan_page._show_current(keep_view=True)
        self.refresh_status()
        self.devices_page.refresh()
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

    def _devices_changed(self) -> None:
        self.print_page.refresh_printers()
        self.scan_page.refresh()
        self.refresh_status()

    def _scan_to_print(self, path: str) -> None:
        self.print_page.add_files([path])
        self.show_page(0)

    def _print_test_page(self, queue: str) -> None:
        toast = self.toasts.task("Page de test en préparation…")
        args = printing.lp_args(queue, "/usr/share/cups/data/testprint", "Page de test")
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
    config.migrate_old_data()
    app = QApplication(argv)
    app.setApplicationName("Feuillet")
    app.setDesktopFileName("feuillet")
    app.setStyle("Fusion")
    theme.set_theme(config.load().get("theme", "sombre"))
    app.setStyleSheet(theme.build_qss())
    window = MainWindow()
    window.show()
    files = [arg for arg in argv[1:] if not arg.startswith("-")]
    if files:
        window.print_page.add_files(files)
    return app.exec()
