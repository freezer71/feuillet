"""Écran « Scanner et PDF » : des pages à la manière de slides, à recadrer, retoucher, trier et exporter."""
from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from . import config, desktop, documents, editor, escl, icons, sane_scan, theme
from .editor import Page
from .widgets import MEDIUM, NARROW, WIDE, Section, ViewSwitch, compact_combo, empty_state, label, panel, run_async, set_kind, to_pixmap

THUMB_SIZE = QSize(120, 165)
HANDLE = 7.0
MIN_DISPLAY = 24.0
MAX_ZOOM = 10.0
PREVIEW_SIDE = 1400
EDGES = {"tl": (-1, -1), "t": (0, -1), "tr": (1, -1), "r": (1, 0), "br": (1, 1), "b": (0, 1), "bl": (-1, 1), "l": (-1, 0)}
CURSORS = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
           "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
           "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
           "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor}


class CropCanvas(QWidget):
    """Le plan de travail : affiche une page, zoome, déplace, recadre et sélectionne du texte."""
    cropEdited = Signal(object)         # (x, y, l, h) en proportions, ou None pour la page entière
    zoomChanged = Signal(float)
    detailWanted = Signal(object, object)   # zone en proportions, taille en pixels
    selectionChanged = Signal(str)

    def __init__(self, title: str, hint: str):
        super().__init__()
        self.setObjectName("preview")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.title, self.hint = title, hint
        self.pixmap: QPixmap | None = None
        self.crop: QRectF | None = None
        self.ratio: float | None = None     # largeur / hauteur imposées par le format
        self.tool = "crop"
        self.zoom = 1.0
        self.pan = QPointF()
        self.words: list[tuple] = []
        self.anchor_word: int | None = None
        self.caret_word: int | None = None
        self.detail: QPixmap | None = None
        self.detail_rect = QRectF()
        self._drag: str | None = None
        self._origin = QPointF()
        self._start = QRectF()
        self._pan_start = QPointF()
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(140)
        self._detail_timer.timeout.connect(self._ask_detail)

    # --- État ----------------------------------------------------------------------------

    def show_page(self, pixmap: QPixmap | None, crop, ratio: float | None, keep_view: bool = False) -> None:
        self.pixmap = pixmap
        self.crop = QRectF(*crop) if crop else None
        self.ratio = ratio
        self._drag = None
        self.detail = None
        if not keep_view:
            self.zoom = 1.0
            self.pan = QPointF()
            self.zoomChanged.emit(self.zoom)
            self.set_words([])
        self._schedule_detail()
        self.update()

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self._drag = None
        self.setCursor({"hand": Qt.CursorShape.OpenHandCursor, "text": Qt.CursorShape.IBeamCursor}.get(tool, Qt.CursorShape.ArrowCursor))
        self.update()

    def set_words(self, words: list[tuple]) -> None:
        self.words = words
        self.anchor_word = self.caret_word = None
        self.selectionChanged.emit("")
        self.update()

    def set_detail(self, pixmap: QPixmap, rect: QRectF) -> None:
        self.detail, self.detail_rect = pixmap, rect
        self.update()

    # --- Géométrie -----------------------------------------------------------------------

    def _fit_rect(self) -> QRectF:
        area = QRectF(self.rect()).adjusted(22, 22, -22, -22)
        size = self.pixmap.size().toSizeF().scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF(area.center().x() - size.width() / 2, area.center().y() - size.height() / 2,
                      size.width(), size.height())

    def image_rect(self) -> QRectF:
        if not self.pixmap:
            return QRectF()
        fit = self._fit_rect()
        width, height = fit.width() * self.zoom, fit.height() * self.zoom
        center = fit.center() + self.pan
        return QRectF(center.x() - width / 2, center.y() - height / 2, width, height)

    def _clamp_pan(self) -> None:
        """La page ne peut pas quitter complètement l'écran."""
        fit = self._fit_rect()
        room_x = max(0.0, (fit.width() * self.zoom - self.width()) / 2 + 40)
        room_y = max(0.0, (fit.height() * self.zoom - self.height()) / 2 + 40)
        self.pan = QPointF(min(room_x, max(-room_x, self.pan.x())), min(room_y, max(-room_y, self.pan.y())))

    def set_zoom(self, zoom: float, around: QPointF | None = None) -> None:
        if not self.pixmap:
            return
        zoom = min(MAX_ZOOM, max(1.0, zoom))
        around = around if around is not None else QPointF(self.rect().center())
        image = self.image_rect()
        relative = QPointF((around.x() - image.x()) / image.width(), (around.y() - image.y()) / image.height())
        self.zoom = zoom
        fit = self._fit_rect()
        width, height = fit.width() * zoom, fit.height() * zoom
        # Le point sous la souris reste au même endroit de l'écran.
        left, top = around.x() - relative.x() * width, around.y() - relative.y() * height
        self.pan = QPointF(left + width / 2 - fit.center().x(), top + height / 2 - fit.center().y())
        self._clamp_pan()
        self.zoomChanged.emit(self.zoom)
        self._schedule_detail()
        self.update()

    def fit(self) -> None:
        self.zoom = 1.0
        self.pan = QPointF()
        self.zoomChanged.emit(self.zoom)
        self.update()

    def visible_rect(self) -> QRectF:
        image = self.image_rect()
        seen = image.intersected(QRectF(self.rect()))
        return QRectF((seen.x() - image.x()) / image.width(), (seen.y() - image.y()) / image.height(),
                      seen.width() / image.width(), seen.height() / image.height())

    def _schedule_detail(self) -> None:
        if self.pixmap and self.image_rect().width() > self.pixmap.width() * 1.05:
            self._detail_timer.start()

    def _ask_detail(self) -> None:
        if not self.pixmap or self.image_rect().width() <= self.pixmap.width() * 1.05:
            return
        image = self.image_rect().intersected(QRectF(self.rect()))
        ratio = self.devicePixelRatioF()
        self.detailWanted.emit(self.visible_rect(), (round(image.width() * ratio), round(image.height() * ratio)))

    def _zone(self) -> QRectF:
        """Le cadre, en pixels à l'écran. Sans recadrage, c'est la page entière."""
        return self._to_screen(self.crop or QRectF(0, 0, 1, 1))

    def _to_screen(self, rect: QRectF) -> QRectF:
        image = self.image_rect()
        return QRectF(image.x() + rect.x() * image.width(), image.y() + rect.y() * image.height(),
                      rect.width() * image.width(), rect.height() * image.height())

    def _handles(self) -> dict[str, QPointF]:
        zone = self._zone()
        points = {}
        for name, (dx, dy) in EDGES.items():
            if self.ratio and 0 in (dx, dy):
                continue    # format imposé : seuls les coins gardent les proportions
            points[name] = QPointF(zone.center().x() + dx * zone.width() / 2, zone.center().y() + dy * zone.height() / 2)
        return points

    def _hit(self, position: QPointF) -> str | None:
        for name, point in self._handles().items():
            if abs(point.x() - position.x()) <= HANDLE + 4 and abs(point.y() - position.y()) <= HANDLE + 4:
                return name
        return "move" if self.crop and self._zone().contains(position) else None

    # --- Texte ---------------------------------------------------------------------------

    def _word_at(self, position: QPointF) -> int | None:
        image = self.image_rect()
        x, y = (position.x() - image.x()) / image.width(), (position.y() - image.y()) / image.height()
        best, distance = None, 0.02 ** 2    # tolérance : un peu à côté d'un mot reste ce mot
        for index, (_text, wx, wy, ww, wh, _line) in enumerate(self.words):
            dx = max(wx - x, 0.0, x - wx - ww)
            dy = max(wy - y, 0.0, y - wy - wh)
            gap = dx * dx + dy * dy
            if gap < distance or (gap == 0 and best is None):
                best, distance = index, gap
        return best

    def selection_range(self) -> range:
        if self.anchor_word is None or self.caret_word is None:
            return range(0)
        low, high = sorted((self.anchor_word, self.caret_word))
        return range(low, high + 1)

    def selected_text(self) -> str:
        parts, last_line = [], None
        for index in self.selection_range():
            text, *_rest, line = self.words[index]
            if last_line is not None:
                parts.append("\n" if line != last_line else " ")
            parts.append(text)
            last_line = line
        return "".join(parts)

    def select_all_text(self) -> None:
        if self.words:
            self.anchor_word, self.caret_word = 0, len(self.words) - 1
            self.selectionChanged.emit(self.selected_text())
            self.update()

    # --- Souris --------------------------------------------------------------------------

    def wheelEvent(self, event):
        if not self.pixmap:
            return
        steps = event.angleDelta().y() / 120
        self.set_zoom(self.zoom * (1.2 ** steps), event.position())
        event.accept()

    def mousePressEvent(self, event):
        if not self.pixmap:
            return
        position = event.position()
        if event.button() == Qt.MouseButton.MiddleButton or (event.button() == Qt.MouseButton.LeftButton and self.tool == "hand"):
            self._drag, self._origin, self._pan_start = "pan", position, self.pan
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.tool == "text":
            word = self._word_at(position)
            self.anchor_word = self.caret_word = word
            self._drag = "text" if word is not None else None
            self.selectionChanged.emit(self.selected_text())
            self.update()
            return
        hit = self._hit(position)
        if hit is None:
            if self.ratio or not self.image_rect().contains(position):
                return
            hit = "new"
        self._drag, self._origin, self._start = hit, position, self._zone()

    def mouseMoveEvent(self, event):
        position = event.position()
        if self._drag == "pan":
            self.pan = self._pan_start + (position - self._origin)
            self._clamp_pan()
            self._schedule_detail()
            self.update()
            return
        if self._drag == "text":
            word = self._word_at(position)
            if word is not None and word != self.caret_word:
                self.caret_word = word
                self.selectionChanged.emit(self.selected_text())
                self.update()
            return
        if self._drag is None:
            if self.tool == "crop":
                hit = self._hit(position) if self.pixmap else None
                self.setCursor(CURSORS.get(hit, Qt.CursorShape.SizeAllCursor if hit == "move" else Qt.CursorShape.ArrowCursor))
            return
        zone = self._dragged(position)
        image = self.image_rect()
        self.crop = QRectF((zone.x() - image.x()) / image.width(), (zone.y() - image.y()) / image.height(),
                           zone.width() / image.width(), zone.height() / image.height())
        self.update()

    def _dragged(self, position: QPointF) -> QRectF:
        image, start = self.image_rect(), self._start
        if self._drag == "move":
            delta = position - self._origin
            x = min(image.right() - start.width(), max(image.left(), start.x() + delta.x()))
            y = min(image.bottom() - start.height(), max(image.top(), start.y() + delta.y()))
            return QRectF(x, y, start.width(), start.height())
        clamped = QPointF(min(image.right(), max(image.left(), position.x())),
                          min(image.bottom(), max(image.top(), position.y())))
        if self._drag == "new":
            zone = QRectF(self._origin, clamped).normalized()
            return zone if zone.width() >= 1 and zone.height() >= 1 else QRectF(zone.topLeft(), QSize(1, 1).toSizeF())
        dx, dy = EDGES[self._drag]
        if self.ratio:
            return self._dragged_locked(clamped, dx, dy)
        left = clamped.x() if dx < 0 else start.left()
        right = clamped.x() if dx > 0 else start.right()
        top = clamped.y() if dy < 0 else start.top()
        bottom = clamped.y() if dy > 0 else start.bottom()
        if dx < 0:
            left = min(left, right - MIN_DISPLAY)
        if dx > 0:
            right = max(right, left + MIN_DISPLAY)
        if dy < 0:
            top = min(top, bottom - MIN_DISPLAY)
        if dy > 0:
            bottom = max(bottom, top + MIN_DISPLAY)
        return QRectF(QPointF(left, top), QPointF(right, bottom))

    def _dragged_locked(self, point: QPointF, dx: int, dy: int) -> QRectF:
        image, start = self.image_rect(), self._start
        anchor_x = start.right() if dx < 0 else start.left()
        anchor_y = start.bottom() if dy < 0 else start.top()
        room_w = anchor_x - image.left() if dx < 0 else image.right() - anchor_x
        room_h = anchor_y - image.top() if dy < 0 else image.bottom() - anchor_y
        width = max(abs(point.x() - anchor_x), abs(point.y() - anchor_y) * self.ratio, MIN_DISPLAY)
        width = min(width, room_w, room_h * self.ratio)
        height = width / self.ratio
        return QRectF(anchor_x - width if dx < 0 else anchor_x, anchor_y - height if dy < 0 else anchor_y, width, height)

    def mouseReleaseEvent(self, event):
        if self._drag is None:
            return
        drag, self._drag = self._drag, None
        if drag == "pan":
            self.set_tool(self.tool)
            return
        if drag == "text":
            return
        zone = self._zone()
        if drag == "new" and (zone.width() < MIN_DISPLAY or zone.height() < MIN_DISPLAY):
            self.crop = None        # simple clic : on garde la page entière
        crop = self.crop
        self.update()
        self.cropEdited.emit((crop.x(), crop.y(), crop.width(), crop.height()) if crop else None)

    # --- Dessin --------------------------------------------------------------------------

    def resizeEvent(self, event):
        self._schedule_detail()
        super().resizeEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not self.pixmap:
            empty_state(painter, self.rect(), "scan", self.title, self.hint)
            return
        image = self.image_rect()
        painter.setClipRect(self.rect().adjusted(1, 1, -1, -1))
        painter.fillRect(image, Qt.GlobalColor.white)
        painter.drawPixmap(image, self.pixmap, QRectF(self.pixmap.rect()))
        if self.detail is not None:
            painter.drawPixmap(self._to_screen(self.detail_rect), self.detail, QRectF(self.detail.rect()))
        zone = self._zone()
        if self.crop:
            shade = QColor(theme.color("shade"))
            painter.fillRect(QRectF(image.x(), image.y(), image.width(), zone.y() - image.y()), shade)
            painter.fillRect(QRectF(image.x(), zone.bottom(), image.width(), image.bottom() - zone.bottom()), shade)
            painter.fillRect(QRectF(image.x(), zone.y(), zone.x() - image.x(), zone.height()), shade)
            painter.fillRect(QRectF(zone.right(), zone.y(), image.right() - zone.right(), zone.height()), shade)
        if self.tool == "text":
            highlight = QColor(theme.color("mark"))
            highlight.setAlpha(80)
            for index in self.selection_range():
                _text, x, y, w, h, _line = self.words[index]
                painter.fillRect(self._to_screen(QRectF(x, y, w, h)), highlight)
        if self.tool == "crop":
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(theme.color("mark")), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(zone)
            painter.setBrush(Qt.GlobalColor.white)
            painter.setPen(QPen(QColor(theme.color("mark")), 1.5))
            for point in self._handles().values():
                painter.drawRect(QRectF(point.x() - HANDLE / 2, point.y() - HANDLE / 2, HANDLE, HANDLE))


class EditorPage(QWidget):
    message = Signal(str, str)
    sendToPrint = Signal(str)
    exportProgress = Signal(int, int)
    scanStage = Signal(str)

    def __init__(self, cfg: dict):
        super().__init__()
        self.setObjectName("page")
        self.cfg = cfg
        self.toasts = None
        self.pages: list[Page] = []
        self.undo_stack: list[list[Page]] = []
        self.redo_stack: list[list[Page]] = []
        self.caps: sane_scan.Capabilities | None = None
        self.folder = editor.data_dir()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.scan_count = 0
        self._thumbs: dict[tuple, tuple[Image.Image, tuple[float, float]]] = {}
        self._previews: OrderedDict[tuple, Image.Image] = OrderedDict()
        self._thumb_queue: list[Page] = []
        self._thumbs_running = False
        self._loading_previews: set[tuple] = set()
        self._current_mm: tuple[float, float] | None = None
        self._detail_token = 0
        self._words_token = 0
        self._scan_toast = None
        self._loading_controls = False
        self.languages = documents.tesseract_langs()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)
        self.view_switch = ViewSwitch("Page")
        self.view_switch.changed.connect(lambda _shown: self._apply_view())
        self.view_switch.hide()
        outer.addWidget(self.view_switch)
        root = QHBoxLayout()
        root.setSpacing(8)
        outer.addLayout(root, 1)
        root.addWidget(self._build_strip())
        self.center_widget = QWidget()
        center = QVBoxLayout(self.center_widget)
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(10)
        center.addWidget(self._build_toolbar())
        self.text_bar = self._build_text_bar()
        center.addWidget(self.text_bar)
        self.canvas = CropCanvas("Aucune page",
                                 "Pose une feuille sur la vitre et clique sur «\u00a0Scanner une page\u00a0», "
                                 "ou glisse ici des PDF et des images pour les fusionner.")
        self.canvas.cropEdited.connect(self._crop_edited)
        self.canvas.zoomChanged.connect(lambda value: self.zoom_label.setText(f"{round(value * 100)} %"))
        self.canvas.detailWanted.connect(self._load_detail)
        self.canvas.selectionChanged.connect(self._text_selected)
        center.addWidget(self.canvas, 1)
        self.page_info = label("", "small")
        center.addWidget(self.page_info)
        root.addWidget(self.center_widget, 1)
        self.side_scroll = self._build_side()
        root.addWidget(self.side_scroll, 1)
        self.layout_mode = WIDE
        self.set_layout_mode(WIDE)
        self._add_shortcuts()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.flush_session)
        self.exportProgress.connect(self._export_progress)
        self.scanStage.connect(self._scan_stage)
        self._export_toast = None
        self._refresh_controls()
        self.refresh()
        self._restore_session()

    # --- Construction ---------------------------------------------------------------------

    def _build_strip(self) -> QFrame:
        frame, layout = panel()
        self.strip_panel = frame
        head = QHBoxLayout()
        self.count_label = label("Pages", "title")
        head.addWidget(self.count_label, 1)
        self.new_button = self._tool_button("new", "", "Nouveau document : vide la liste des pages", checkable=False)
        self.new_button.clicked.connect(self.new_document)
        head.addWidget(self.new_button)
        layout.addLayout(head)
        self.strip = QListWidget()
        self.strip.setObjectName("slides")
        self.strip.setViewMode(QListWidget.ViewMode.ListMode)
        self.strip.setFlow(QListWidget.Flow.TopToBottom)
        self.strip.setIconSize(THUMB_SIZE)
        self.strip.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.strip.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.strip.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.strip.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.strip.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.strip.currentRowChanged.connect(lambda _row: self._show_current())
        self.strip.itemSelectionChanged.connect(self._refresh_controls)
        self.strip.model().rowsMoved.connect(self._reordered)
        layout.addWidget(self.strip, 1)
        self.up_button = self._tool_button("up", "", "Monter", checkable=False)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button = self._tool_button("down", "", "Descendre", checkable=False)
        self.down_button.clicked.connect(lambda: self._move(1))
        self.duplicate_button = self._tool_button("duplicate", "", "Dupliquer", checkable=False)
        self.duplicate_button.clicked.connect(self._duplicate)
        self.delete_button = self._tool_button("trash", "", "Supprimer (touche Suppr)", checkable=False)
        self.delete_button.clicked.connect(self._delete)
        self.strip_buttons = QGridLayout()
        self.strip_buttons.setSpacing(4)
        layout.addLayout(self.strip_buttons)
        return frame

    def _tool_button(self, name: str, text: str, tip: str, checkable: bool = True) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("tool")
        button.setToolTip(tip)
        button.setCheckable(checkable)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        icons.set_icon(button, name)
        return button

    def _build_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("toolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(4)
        self.tools = {}
        self.tool_specs = {}
        for key, name, text, tip in (("crop", "crop", "Recadrer", "Tracer et ajuster le cadre de la page"),
                                     ("hand", "hand", "Déplacer", "Se déplacer dans la page zoomée (ou bouton du milieu)"),
                                     ("text", "text", "Texte", "Sélectionner le texte de la page pour le copier")):
            button = self._tool_button(name, text, tip)
            button.clicked.connect(lambda _checked=False, k=key: self.set_tool(k))
            row.addWidget(button)
            self.tools[key] = button
            self.tool_specs[key] = (name, text, tip)
        self.tools["crop"].setChecked(True)
        row.addStretch(1)
        out = self._tool_button("minus", "", "Dézoomer (Ctrl+-)", checkable=False)
        out.clicked.connect(lambda: self.canvas.set_zoom(self.canvas.zoom / 1.25))
        self.zoom_label = QLabel("100 %")
        self.zoom_label.setFixedWidth(48)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        zoom_in = self._tool_button("plus", "", "Zoomer (Ctrl++), ou molette", checkable=False)
        zoom_in.clicked.connect(lambda: self.canvas.set_zoom(self.canvas.zoom * 1.25))
        fit = self._tool_button("fit", "", "Voir toute la page (Ctrl+0)", checkable=False)
        fit.clicked.connect(lambda: self.canvas.fit())
        for widget in (out, self.zoom_label, zoom_in, fit):
            row.addWidget(widget)
        return bar

    def _build_text_bar(self) -> QFrame:
        """N'apparaît qu'avec l'outil Texte."""
        bar = QFrame()
        bar.setObjectName("toolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 4, 8, 4)
        row.setSpacing(4)
        self.text_state = label("", "small")
        row.addWidget(self.text_state, 1)
        self.select_all_button = QPushButton("Tout sélectionner")
        self.select_all_button.setObjectName("tool")
        self.select_all_button.clicked.connect(self.canvas_select_all)
        self.copy_button = self._tool_button("copy", "Copier", "Copier le texte sélectionné (Ctrl+C)", checkable=False)
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_text)
        row.addWidget(self.select_all_button)
        row.addWidget(self.copy_button)
        bar.hide()
        return bar

    def _form(self) -> QFormLayout:
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(12)
        return form

    def _build_side(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side, layout = panel()
        layout.setSpacing(8)

        # Ajouter
        box = QWidget()
        inner = QVBoxLayout(box)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(10)
        self.state = label("Vérification du scanner…", "small")
        inner.addWidget(self.state)
        form = self._form()
        self.scan_form = form
        self.scanner_box = compact_combo()
        self.scanner_box.activated.connect(self._scanner_chosen)
        form.addRow("Scanner", self.scanner_box)
        self.source = compact_combo()
        form.addRow("Source", self.source)
        self.area = compact_combo()
        self.color = compact_combo()
        self.resolution = compact_combo()
        self.resolution.currentIndexChanged.connect(self._resolution_hint)
        form.addRow("Zone", self.area)
        form.addRow("Couleur", self.color)
        form.addRow("Résolution", self.resolution)
        inner.addLayout(form)
        self.hint = label("", "warn")
        self.hint.hide()
        inner.addWidget(self.hint)
        self.scan_button = QPushButton("Scanner une page")
        self.scan_button.setObjectName("primary")
        self.scan_button.setToolTip("Raccourci : F5")
        icons.set_icon(self.scan_button, "scan", 18, "on_primary")
        self.scan_button.clicked.connect(self.scan)
        inner.addWidget(self.scan_button)
        row = QHBoxLayout()
        add_files = QPushButton("Ajouter des fichiers")
        icons.set_icon(add_files, "file")
        add_files.clicked.connect(self.choose_files)
        add_blank = QPushButton("Page vierge")
        add_blank.clicked.connect(self._add_blank)
        row.addWidget(add_files, 1)
        row.addWidget(add_blank)
        inner.addLayout(row)
        layout.addWidget(Section("Ajouter des pages", box))

        # Recadrage
        box = QWidget()
        inner = QVBoxLayout(box)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(8)
        form = self._form()
        self.format_box = compact_combo()
        for name, value in editor.FORMATS.items():
            self.format_box.addItem(name, value)
        self.format_box.activated.connect(self._format_chosen)
        form.addRow("Format", self.format_box)
        inner.addLayout(form)
        row = QHBoxLayout()
        self.left_button = QPushButton("Gauche")
        icons.set_icon(self.left_button, "rotate_left")
        self.left_button.clicked.connect(lambda: self._rotate(270))
        self.right_button = QPushButton("Droite")
        icons.set_icon(self.right_button, "rotate_right")
        self.right_button.clicked.connect(lambda: self._rotate(90))
        row.addWidget(self.left_button)
        row.addWidget(self.right_button)
        inner.addLayout(row)
        self.auto_crop_button = QPushButton("Recadrer automatiquement")
        icons.set_icon(self.auto_crop_button, "wand")
        self.auto_crop_button.setToolTip("Coupe les bords vides autour du contenu des pages sélectionnées")
        self.auto_crop_button.clicked.connect(self._auto_crop)
        inner.addWidget(self.auto_crop_button)
        self.reset_button = QPushButton("Retirer le cadre")
        self.reset_button.clicked.connect(self._reset_crop)
        inner.addWidget(self.reset_button)
        layout.addWidget(Section("Recadrage et rotation", box))

        # Retouches
        box = QWidget()
        inner = QVBoxLayout(box)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(8)
        form = self._form()
        self.filter_box = compact_combo()
        for key, name in editor.FILTERS.items():
            self.filter_box.addItem(name, key)
        self.filter_box.activated.connect(self._filter_chosen)
        form.addRow("Rendu", self.filter_box)
        self.brightness = self._slider(-100, 100)
        self.contrast = self._slider(-100, 100)
        self.angle = self._slider(-150, 150)
        form.addRow("Luminosité", self.brightness)
        form.addRow("Contraste", self.contrast)
        form.addRow("Redresser", self.angle)
        inner.addLayout(form)
        self.deskew_button = QPushButton("Redresser automatiquement")
        icons.set_icon(self.deskew_button, "wand")
        self.deskew_button.clicked.connect(self._auto_deskew)
        inner.addWidget(self.deskew_button)
        self.apply_all_button = QPushButton("Appliquer à toutes les pages")
        self.apply_all_button.setToolTip("Reprend le cadre, le format et le rendu de cette page")
        self.apply_all_button.clicked.connect(self._apply_to_all)
        inner.addWidget(self.apply_all_button)
        layout.addWidget(Section("Retouches", box, opened=False))

        # Historique
        row = QHBoxLayout()
        self.undo_button = QPushButton("Annuler")
        icons.set_icon(self.undo_button, "undo")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button = QPushButton("Rétablir")
        icons.set_icon(self.redo_button, "redo")
        self.redo_button.clicked.connect(self.redo)
        row.addWidget(self.undo_button)
        row.addWidget(self.redo_button)
        layout.addLayout(row)

        # Export
        box = QWidget()
        inner = QVBoxLayout(box)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(8)
        form = self._form()
        self.quality = compact_combo()
        self.quality.addItems(list(editor.QUALITIES))
        self.quality.setToolTip("Réduit le poids des scans. Les pages de PDF sans retouche restent intactes.")
        form.addRow("Qualité", self.quality)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Aucun")
        form.addRow("Mot de passe", self.password)
        inner.addLayout(form)
        self.ocr = QCheckBox("Texte cherchable dans le PDF (OCR)")
        self.ocr.setEnabled(bool(self.languages))
        self.ocr.setToolTip("Les pages sont converties en images à 300 dpi, puis le texte y est reconnu.")
        inner.addWidget(self.ocr)
        self.export_button = QPushButton("Exporter en PDF")
        self.export_button.setObjectName("primary")
        icons.set_icon(self.export_button, "export", 18, "on_primary")
        self.export_button.clicked.connect(lambda: self.export(False))
        self.export_selection_button = QPushButton("Exporter la sélection")
        self.export_selection_button.clicked.connect(lambda: self.export(True))
        self.export_image_button = QPushButton("Exporter la page en image")
        self.export_image_button.clicked.connect(self._export_image)
        self.print_button = QPushButton("Envoyer vers Imprimer")
        icons.set_icon(self.print_button, "print")
        self.print_button.clicked.connect(self._to_print)
        for button in (self.export_button, self.export_selection_button, self.export_image_button, self.print_button):
            inner.addWidget(button)
        if editor.pypdf is None:
            missing = label(editor.PYPDF_HINT, "warn")
            missing.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            inner.addWidget(missing)
        layout.addWidget(Section("Exporter", box))
        layout.addStretch(1)
        scroll.setWidget(side)
        return scroll

    def set_layout_mode(self, mode: str) -> None:
        self.layout_mode = mode
        widths = {WIDE: (210, 300, QSize(120, 165)), MEDIUM: (160, 270, QSize(96, 132)), NARROW: (104, 0, QSize(64, 88))}
        strip, side, icon = widths[mode]
        self.strip_panel.setFixedWidth(strip)
        # Le nom des outils demande de la place : sinon, une icône et son nom en infobulle.
        for key, button in self.tools.items():
            name, text, _tip = self.tool_specs[key]
            if mode == WIDE:
                icons.forget(button)
                button.setIcon(QIcon())
                button.setText(text)
            else:
                button.setText("")
                icons.set_icon(button, name)
        self.strip.setIconSize(icon)
        self.strip.doItemsLayout()
        if side:
            self.side_scroll.setFixedWidth(side)
        else:
            self.side_scroll.setMinimumWidth(0)
            self.side_scroll.setMaximumWidth(16777215)
        columns = 2 if mode == NARROW else 4
        buttons = (self.up_button, self.down_button, self.duplicate_button, self.delete_button)
        for index, button in enumerate(buttons):
            self.strip_buttons.addWidget(button, index // columns, index % columns)
        self.view_switch.setVisible(mode == NARROW)
        self._apply_view()

    def _apply_view(self) -> None:
        narrow = self.layout_mode == NARROW
        settings = narrow and self.view_switch.showing_settings()
        self.center_widget.setVisible(not settings)
        self.strip_panel.setVisible(not settings)
        self.side_scroll.setVisible(not narrow or settings)

    def _slider(self, low: int, high: int) -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.sliderPressed.connect(self._snapshot)
        slider.valueChanged.connect(self._sliders_changed)
        return slider

    def _add_shortcuts(self) -> None:
        def add(owner, keys, handler, context):
            action = QAction(owner)
            action.setShortcut(keys)
            action.setShortcutContext(context)
            action.triggered.connect(handler)
            owner.addAction(action)

        # Réservés aux zones de travail : un champ de saisie garde ses propres raccourcis.
        widget_only = Qt.ShortcutContext.WidgetShortcut
        for owner in (self.strip, self.canvas):
            add(owner, QKeySequence.StandardKey.Undo, self.undo, widget_only)
            add(owner, QKeySequence.StandardKey.Redo, self.redo, widget_only)
            add(owner, QKeySequence.StandardKey.Copy, self.copy_text, widget_only)
        add(self.strip, QKeySequence.StandardKey.Delete, self._delete, widget_only)
        page_wide = Qt.ShortcutContext.WidgetWithChildrenShortcut
        add(self, QKeySequence("F5"), self.scan, page_wide)
        add(self, QKeySequence("Ctrl+0"), lambda: self.canvas.fit(), page_wide)
        add(self, QKeySequence.StandardKey.ZoomIn, lambda: self.canvas.set_zoom(self.canvas.zoom * 1.25), page_wide)
        add(self, QKeySequence.StandardKey.ZoomOut, lambda: self.canvas.set_zoom(self.canvas.zoom / 1.25), page_wide)

    # --- Session --------------------------------------------------------------------------

    def _restore_session(self) -> None:
        pages, count, missing = editor.load_session(self.folder)
        self.scan_count = count
        if pages:
            self.pages = pages
            self._rebuild(0)
        if missing:
            QTimer.singleShot(0, lambda: self.message.emit(
                f"{missing} page(s) de la session précédente ont disparu : leur fichier d'origine n'existe plus.", "warn"))

    def flush_session(self) -> None:
        try:
            editor.save_session(self.pages, self.folder, self.scan_count)
        except OSError as error:
            self.message.emit(f"Sauvegarde de la session impossible : {error}", "error")

    def _changed(self) -> None:
        self._save_timer.start()

    def new_document(self) -> None:
        if self.pages:
            answer = QMessageBox.question(self, "Nouveau document",
                                          f"Retirer les {len(self.pages)} pages de la liste ?\n\n"
                                          "Les scans non exportés seront définitivement supprimés.")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.pages, self.undo_stack, self.redo_stack = [], [], []
        self.scan_count = 0
        self._thumbs.clear()
        self._previews.clear()
        editor.forget_detail()
        self._rebuild(-1)
        self._save_timer.stop()
        try:
            editor.clear_session(self.folder)
        except OSError as error:
            self.message.emit(f"Nettoyage incomplet : {error}", "warn")

    # --- Scanner ---------------------------------------------------------------------------

    def current_scanner(self) -> dict | None:
        scanners = self.cfg["scanners"]
        chosen = self.cfg.get("scanner")
        return next((item for item in scanners if item["id"] == chosen), scanners[0] if scanners else None)

    def _scanner_chosen(self) -> None:
        self.cfg["scanner"] = self.scanner_box.currentData()
        config.save(self.cfg)
        self.refresh()

    def refresh(self) -> None:
        scanner = self.current_scanner()
        self.scanner_box.clear()
        for item in self.cfg["scanners"]:
            self.scanner_box.addItem(item["name"], item["id"])
        self.scan_form.setRowVisible(self.scanner_box, len(self.cfg["scanners"]) > 1)
        if scanner is None:
            self.caps = None
            set_kind(self.state, "Aucun scanner. Ajoute-en un dans Réglages.", "warn")
            self.scan_form.setRowVisible(self.source, False)
            self.scan_button.setEnabled(False)
            return
        self.scanner_box.setCurrentIndex(max(0, self.scanner_box.findData(scanner["id"])))
        if self.scan_button.text() != "Scan en cours…":
            self.scan_button.setEnabled(True)

        def query():
            caps = sane_scan.capabilities(scanner)
            state = ""
            if scanner.get("host"):
                try:
                    state = escl.Scanner(scanner["host"]).state()
                except (OSError, escl.ScanError, ValueError):
                    state = ""     # l'état est un plus : tous les appareils ne le donnent pas
            return scanner["id"], caps, state

        def ready(result):
            scanner_id, caps, state = result
            if scanner_id != (self.current_scanner() or {}).get("id"):
                return
            self.caps = caps
            self._fill_scan_options(caps)
            names = {"Idle": ("prêt", "ok"), "Processing": ("occupé", "warn"), "Stopped": ("arrêté", "error")}
            text, kind = names.get(state, ("prêt", "ok"))
            set_kind(self.state, f"{scanner['name']} : {text}", kind)

        def failed(text):
            self.caps = None
            set_kind(self.state, text, "error")

        run_async(self, query, done=ready, failed=failed)

    def _fill_scan_options(self, caps: sane_scan.Capabilities) -> None:
        def fill(box, values, default):
            current = box.currentData() if box.currentData() is not None else default
            box.blockSignals(True)
            box.clear()
            for value in values:
                box.addItem(f"{value} dpi" if isinstance(value, int) else value, value)
            index = box.findData(current)
            box.setCurrentIndex(index if index >= 0 else 0)
            box.blockSignals(False)

        fill(self.area, caps.areas, "A4")
        fill(self.color, caps.colors, sane_scan.COLOR)
        wanted = 300 if 300 in caps.resolutions else None
        fill(self.resolution, caps.resolutions, wanted)
        fill(self.source, caps.sources, None)
        self.scan_form.setRowVisible(self.source, len(caps.sources) > 1)
        self._resolution_hint()

    def _resolution_hint(self) -> None:
        value = self.resolution.currentData() or 0
        self.hint.setVisible(value >= 600)
        self.hint.setText("Haute résolution : le scan prend plus de temps et le fichier sera lourd." if value >= 600 else "")

    def _scan_stage(self, text: str) -> None:
        if self._scan_toast:
            self._scan_toast.update_text(text)

    def scan(self) -> None:
        scanner, caps = self.current_scanner(), self.caps
        if not self.scan_button.isEnabled() or not scanner or not caps:
            return
        area = self.area.currentData()
        color_name = self.color.currentData()
        resolution = self.resolution.currentData()
        source = self.source.currentData() if len(caps.sources) > 1 else ""
        folder = self.folder
        number = self.scan_count + 1
        stage = self.scanStage

        def job():
            image = sane_scan.scan(scanner, caps, resolution, color_name, area, source or "")
            stage.emit("Enregistrement du scan…")
            if color_name == sane_scan.GRAY:
                image = image.convert("L")
            elif color_name == sane_scan.BLACK_WHITE:
                image = image.convert("L")
                limit = editor.otsu_threshold(image)
                image = image.point(lambda v: 255 if v > limit else 0, mode="1")
            size_mm = (image.width / resolution * editor.MM_PER_INCH, image.height / resolution * editor.MM_PER_INCH)
            # L'aperçu se calcule ici, avec l'image déjà en mémoire : rien ne bloque l'interface.
            preview, thumb = _downscaled(image)
            return editor.page_from_scan(image, resolution, folder, number), preview, thumb, size_mm

        self.scan_button.setEnabled(False)
        self.scan_button.setText("Scan en cours…")
        self._scan_toast = self._task(f"Numérisation à {resolution} dpi…")
        run_async(self, job, done=self._scanned, failed=self._scan_failed)

    def _task(self, text: str):
        return self.toasts.task(text) if self.toasts else None

    def _finish_task(self, toast, text: str, kind: str = "ok", actions=None) -> None:
        if toast:
            toast.finish(text, kind, actions)
        else:
            self.message.emit(text, kind)

    def _end_scan(self) -> None:
        self.scan_button.setEnabled(True)
        self.scan_button.setText("Scanner la page suivante" if self.pages else "Scanner une page")

    def _scanned(self, result) -> None:
        page, preview, thumb, size_mm = result
        key = self._base_key(page)
        self._thumbs[key] = (thumb, size_mm)
        self._remember_preview(key, preview)
        self._snapshot()
        self.scan_count += 1
        self.pages.append(page)
        self._rebuild(len(self.pages) - 1)
        self.strip.scrollToBottom()
        self._end_scan()
        self._finish_task(self._scan_toast, f"{page.name} ajouté. Pose la feuille suivante.")
        self._scan_toast = None

    def _scan_failed(self, text: str) -> None:
        self._end_scan()
        self._finish_task(self._scan_toast, text, "error")
        self._scan_toast = None

    # --- Pages ----------------------------------------------------------------------------

    def choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Ajouter des fichiers", str(Path.home()), documents.OPEN_FILTER)
        self.add_files(paths)

    def add_files(self, paths: list[str]) -> None:
        for path in paths:
            run_async(self, editor.pages_from_file, path, self.folder, done=self._files_added,
                      failed=lambda text, p=path: self.message.emit(f"{Path(p).name} : {text}", "error"))

    def _files_added(self, pages: list[Page]) -> None:
        self._snapshot()
        first = len(self.pages)
        self.pages.extend(pages)
        self._rebuild(first)
        self.message.emit(f"{len(pages)} page(s) ajoutée(s) depuis {pages[0].name}.", "ok")

    def _add_blank(self) -> None:
        self.scan_count += 1
        page = editor.blank_page(self.folder, self.scan_count)
        self._snapshot()
        row = self.strip.currentRow() + 1 if self.pages else 0
        self.pages.insert(row, page)
        self._rebuild(row)

    def _snapshot(self) -> None:
        self.undo_stack.append(list(self.pages))
        del self.undo_stack[:-100]
        self.redo_stack.clear()

    def undo(self) -> None:
        if self.undo_stack:
            self.redo_stack.append(list(self.pages))
            self.pages = self.undo_stack.pop()
            self._rebuild(self.strip.currentRow())

    def redo(self) -> None:
        if self.redo_stack:
            self.undo_stack.append(list(self.pages))
            self.pages = self.redo_stack.pop()
            self._rebuild(self.strip.currentRow())

    def _rows(self) -> list[int]:
        return sorted(self.strip.row(item) for item in self.strip.selectedItems())

    def current(self) -> Page | None:
        row = self.strip.currentRow()
        return self.pages[row] if 0 <= row < len(self.pages) else None

    def _rebuild(self, current: int = -1, selected: list[int] | None = None) -> None:
        self.strip.blockSignals(True)
        self.strip.model().blockSignals(True)
        self.strip.clear()
        for number, page in enumerate(self.pages, start=1):
            item = QListWidgetItem(self._icon(page), str(number))
            item.setData(Qt.ItemDataRole.UserRole, page.uid)
            item.setToolTip(page.name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.strip.addItem(item)
        self.strip.model().blockSignals(False)
        self.strip.blockSignals(False)
        self.strip.doItemsLayout()
        self.strip.viewport().update()
        row = min(current, len(self.pages) - 1)
        if row < 0 and self.pages:
            row = 0
        self.strip.blockSignals(True)
        self.strip.setCurrentRow(row)
        self.strip.blockSignals(False)
        if selected:
            self.strip.clearSelection()
            for index in selected:
                if 0 <= index < len(self.pages):
                    self.strip.item(index).setSelected(True)
        self.count_label.setText(f"Pages ({len(self.pages)})" if self.pages else "Pages")
        self._show_current()
        self._changed()

    def _reordered(self, *_args) -> None:
        by_uid = {page.uid: page for page in self.pages}
        order = [self.strip.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.strip.count())]
        moved = [by_uid[uid] for uid in order]
        if moved != self.pages:
            self._snapshot()
            self.pages = moved
            self._changed()
        for index in range(self.strip.count()):
            self.strip.item(index).setText(str(index + 1))
        self._refresh_controls()

    def _move(self, delta: int) -> None:
        rows = self._rows()
        if not rows or (delta < 0 and rows[0] == 0) or (delta > 0 and rows[-1] == len(self.pages) - 1):
            return
        self._snapshot()
        chosen = set(rows)
        for row in (rows if delta < 0 else reversed(rows)):
            self.pages[row], self.pages[row + delta] = self.pages[row + delta], self.pages[row]
        moved = [row + delta for row in rows]
        current = self.strip.currentRow() + delta if self.strip.currentRow() in chosen else moved[0]
        self._rebuild(current, moved)

    def _delete(self) -> None:
        rows = self._rows()
        if not rows:
            return
        self._snapshot()
        for row in reversed(rows):
            del self.pages[row]
        self._rebuild(rows[0])

    def _duplicate(self) -> None:
        rows = self._rows()
        if not rows:
            return
        self._snapshot()
        copies = [replace(self.pages[row], uid=editor.new_uid()) for row in rows]
        self.pages[rows[-1] + 1:rows[-1] + 1] = copies
        self._rebuild(rows[-1] + 1)

    # --- Rendu ----------------------------------------------------------------------------

    @staticmethod
    def _base_key(page: Page) -> tuple:
        return page.source, page.index

    @staticmethod
    def _as_icon(pixmap: QPixmap) -> QIcon:
        """Sans mode « sélectionné » explicite, Qt teinte la vignette et fausse ses couleurs."""
        framed = QPixmap(pixmap)
        painter = QPainter(framed)
        painter.setPen(QColor(theme.color("border")))
        painter.drawRect(framed.rect().adjusted(0, 0, -1, -1))
        painter.end()
        icon = QIcon(framed)
        icon.addPixmap(framed, QIcon.Mode.Selected)
        return icon

    def _icon(self, page: Page) -> QIcon:
        base = self._thumbs.get(self._base_key(page))
        if base is None:
            if page not in self._thumb_queue:
                self._thumb_queue.append(page)
            self._next_thumb()
            blank = QPixmap(THUMB_SIZE)
            blank.fill(QColor(theme.color("raised")))
            return self._as_icon(blank)
        image = editor.apply_view(base[0], page)
        if page.crop:
            image = documents.crop_normalized(image, page.crop)
        small = image.convert("RGB").copy()
        small.thumbnail((THUMB_SIZE.width(), THUMB_SIZE.height()))
        return self._as_icon(to_pixmap(small))

    def _remember_preview(self, key: tuple, image: Image.Image) -> None:
        self._previews[key] = image
        self._previews.move_to_end(key)
        while len(self._previews) > 6:
            self._previews.popitem(last=False)

    @staticmethod
    def _load_base(page: Page):
        image, dpi = editor.render_base(page, 110)
        size_mm = (image.width / dpi * editor.MM_PER_INCH, image.height / dpi * editor.MM_PER_INCH)
        preview, thumb = _downscaled(image)
        return (page.source, page.index), preview, thumb, size_mm

    def _next_thumb(self) -> None:
        if self._thumbs_running or not self._thumb_queue:
            return
        page = self._thumb_queue.pop(0)
        if self._base_key(page) in self._thumbs:
            self._next_thumb()
            return
        self._thumbs_running = True

        def finish(result):
            self._thumbs_running = False
            key, preview, thumb, size_mm = result
            self._thumbs[key] = (thumb, size_mm)
            self._remember_preview(key, preview)
            self._update_icons(key)
            self._next_thumb()

        def failed(text):
            self._thumbs_running = False
            self.message.emit(text, "error")
            self._next_thumb()

        run_async(self, self._load_base, page, done=finish, failed=failed)

    def _update_icons(self, key: tuple) -> None:
        for row, page in enumerate(self.pages):
            if self._base_key(page) == key and row < self.strip.count():
                self.strip.item(row).setIcon(self._icon(page))
        current = self.current()
        if current and self._base_key(current) == key:
            self._show_current()

    def _refresh_thumb(self, row: int) -> None:
        if 0 <= row < self.strip.count():
            self.strip.item(row).setIcon(self._icon(self.pages[row]))

    def _show_current(self, keep_view: bool = False) -> None:
        page = self.current()
        self._refresh_controls()
        if not page:
            self.canvas.show_page(None, None, None)
            self.page_info.setText("")
            self._current_mm = None
            self.text_state.setText("")
            return
        key = self._base_key(page)
        known = self._thumbs.get(key)
        if known is None:
            self._icon(page)    # lance le rendu ; l'affichage suivra à son arrivée
            self.page_info.setText("Chargement de la page…")
            return
        preview = self._previews.get(key)
        if preview is None:
            preview = known[0]  # la vignette patiente le temps de relire l'aperçu
            self._reload_preview(page, key)
        else:
            self._previews.move_to_end(key)
        size_mm = known[1]
        self._current_mm = size_mm if page.rotation in (0, 180) else (size_mm[1], size_mm[0])
        ratio = page.target[0] / page.target[1] if page.target else None
        view = editor.apply_view(preview, page)
        self.canvas.show_page(to_pixmap(view.convert("RGB") if view.mode == "1" else view), page.crop, ratio, keep_view)
        self._update_info(page)
        if not keep_view and self.canvas.tool == "text":
            self._load_words()

    def _reload_preview(self, page: Page, key: tuple) -> None:
        if key in self._loading_previews:
            return
        self._loading_previews.add(key)

        def finish(result):
            self._loading_previews.discard(key)
            _key, preview, _thumb, _size = result
            self._remember_preview(key, preview)
            current = self.current()
            if current and self._base_key(current) == key:
                self._show_current(keep_view=True)

        def failed(text):
            self._loading_previews.discard(key)
            self.message.emit(text, "error")

        run_async(self, self._load_base, page, done=finish, failed=failed)

    def _load_detail(self, rect: QRectF, size) -> None:
        page = self.current()
        if not page:
            return
        self._detail_token += 1
        token = self._detail_token
        area = (rect.x(), rect.y(), rect.width(), rect.height())

        def show(image):
            if token == self._detail_token and self.current() is page:
                self.canvas.set_detail(to_pixmap(image), rect)

        run_async(self, editor.render_detail, page, area, size, done=show, failed=lambda _text: None)

    def _page_ratio(self) -> float:
        return self._current_mm[0] / self._current_mm[1]

    def _update_info(self, page: Page) -> None:
        if not self._current_mm:
            return
        width, height = self._current_mm
        if page.crop:
            width, height = width * page.crop[2], height * page.crop[3]
        text = f"Page {self.strip.currentRow() + 1} sur {len(self.pages)}  ·  {page.name}  ·  zone {width:.0f} × {height:.0f} mm"
        if page.dpi:
            text += f"  ·  {page.dpi} dpi"
        if page.target:
            text += f"  ·  exportée en {page.target[0]:g} × {page.target[1]:g} mm"
        self.page_info.setText(text)

    def _refresh_controls(self) -> None:
        page = self.current()
        rows = self._rows()
        has_pages = bool(self.pages)
        for button in (self.up_button, self.down_button, self.duplicate_button, self.delete_button,
                       self.export_selection_button):
            button.setEnabled(bool(rows))
        for widget in (self.left_button, self.right_button, self.apply_all_button, self.export_image_button,
                       self.auto_crop_button, self.deskew_button, self.format_box, self.filter_box,
                       self.brightness, self.contrast, self.angle):
            widget.setEnabled(page is not None)
        self.reset_button.setEnabled(page is not None and (page.crop is not None or page.target is not None))
        self.export_button.setEnabled(has_pages and editor.pypdf is not None)
        self.export_selection_button.setEnabled(bool(rows) and editor.pypdf is not None)
        self.print_button.setEnabled(has_pages and editor.pypdf is not None)
        self.undo_button.setEnabled(bool(self.undo_stack))
        self.redo_button.setEnabled(bool(self.redo_stack))
        self.new_button.setEnabled(has_pages)
        if page:
            self._loading_controls = True
            index = self.format_box.findData(page.target)
            self.format_box.setCurrentIndex(index if index >= 0 else 0)
            self.filter_box.setCurrentIndex(max(0, self.filter_box.findData(page.filter)))
            self.brightness.setValue(page.brightness)
            self.contrast.setValue(page.contrast)
            self.angle.setValue(round(page.angle * 10))
            self._loading_controls = False

    # --- Outils ---------------------------------------------------------------------------

    def set_tool(self, tool: str) -> None:
        for key, button in self.tools.items():
            button.setChecked(key == tool)
        self.canvas.set_tool(tool)
        is_text = tool == "text"
        self.text_bar.setVisible(is_text)
        if is_text:
            self._load_words()
        else:
            self.text_state.setText("")

    def _load_words(self) -> None:
        page = self.current()
        if not page:
            return
        self._words_token += 1
        token = self._words_token
        lang = self._ocr_lang_list()
        needs_ocr = not page.is_pdf or bool(page.angle)
        self.text_state.setText("Lecture du texte de la page…" if needs_ocr else "")
        toast = self._task("Lecture du texte de la page…") if needs_ocr else None

        def show(result):
            words, recognized = result
            if toast:
                toast.dismiss() if toast.finished else toast.finish(f"{len(words)} mots reconnus." if words else "Aucun texte trouvé.",
                                                                    "ok" if words else "warn")
            if token != self._words_token:
                return
            self.canvas.set_words(words)
            if words:
                origin = "reconnus" if recognized else "trouvés"
                self.text_state.setText(f"{len(words)} mots {origin} : glisse sur le texte pour le sélectionner.")
            else:
                self.text_state.setText("Aucun texte trouvé sur cette page.")

        def failed(text):
            self._finish_task(toast, text, "error")
            if token == self._words_token:
                self.text_state.setText("")

        run_async(self, editor.page_words, page, lang, done=show, failed=failed)

    def _text_selected(self, text: str) -> None:
        self.copy_button.setEnabled(bool(text))

    def canvas_select_all(self) -> None:
        self.canvas.select_all_text()

    def copy_text(self) -> None:
        text = self.canvas.selected_text()
        if text:
            QApplication.clipboard().setText(text)
            self.message.emit(f"Texte copié ({len(text.split())} mots).", "ok")

    # --- Retouches ------------------------------------------------------------------------

    def _replace_current(self, page: Page, keep_view: bool = True) -> None:
        row = self.strip.currentRow()
        self.pages[row] = page
        self._refresh_thumb(row)
        self._show_current(keep_view)
        self._changed()

    def _crop_edited(self, crop) -> None:
        page = self.current()
        if page and page.crop != crop:
            self._snapshot()
            self._replace_current(replace(page, crop=crop))

    def _format_chosen(self) -> None:
        page = self.current()
        if not page or not self._current_mm:
            return
        target = self.format_box.currentData()
        if page.target == target:
            return
        self._snapshot()
        self._replace_current(editor.with_format(page, target, self._page_ratio()))

    def _rotate(self, degrees: int) -> None:
        rows = self._rows() or [self.strip.currentRow()]
        if rows == [-1]:
            return
        self._snapshot()
        for row in rows:
            self.pages[row] = editor.rotated(self.pages[row], degrees)
            self._refresh_thumb(row)
        self._show_current()
        self._changed()

    def _reset_crop(self) -> None:
        page = self.current()
        if page:
            self._snapshot()
            self._replace_current(replace(page, crop=None, target=None))

    def _filter_chosen(self) -> None:
        page = self.current()
        value = self.filter_box.currentData()
        if page and page.filter != value:
            self._snapshot()
            self._replace_current(replace(page, filter=value))

    def _sliders_changed(self) -> None:
        page = self.current()
        if self._loading_controls or not page:
            return
        self._replace_current(replace(page, brightness=self.brightness.value(), contrast=self.contrast.value(),
                                      angle=self.angle.value() / 10))

    def _auto_crop(self) -> None:
        rows = self._rows() or [self.strip.currentRow()]
        if rows == [-1]:
            return
        pages = [self.pages[row] for row in rows]

        def job():
            found = []
            for page in pages:
                base, _dpi = editor.render_base(page, 100)
                found.append(editor.detect_content(editor.apply_view(base, page)))
            return found

        toast = self._task("Recherche des bords…")

        def done(found):
            self._snapshot()
            changed = 0
            for row, rect in zip(rows, found):
                if rect:
                    self.pages[row] = replace(self.pages[row], crop=rect, target=None)
                    self._refresh_thumb(row)
                    changed += 1
            self._show_current(True)
            self._changed()
            self._finish_task(toast, f"{changed} page(s) recadrée(s)." if changed else "Aucun contenu détecté.",
                              "ok" if changed else "warn")

        run_async(self, job, done=done, failed=lambda text: self._finish_task(toast, text, "error"))

    def _auto_deskew(self) -> None:
        page = self.current()
        if not page:
            return
        row = self.strip.currentRow()
        toast = self._task("Analyse de l'inclinaison…")

        def job():
            base, _dpi = editor.render_base(page, 100)
            return editor.detect_skew(editor.apply_rotation(base.convert("RGB"), page.rotation))

        def done(angle):
            if row < len(self.pages) and self.pages[row].uid == page.uid:
                self._snapshot()
                self.pages[row] = replace(self.pages[row], angle=angle)
                self._refresh_thumb(row)
                self._show_current(True)
                self._changed()
            self._finish_task(toast, f"Page redressée de {angle:+.1f}°." if angle else "La page est déjà droite.")

        run_async(self, job, done=done, failed=lambda text: self._finish_task(toast, text, "error"))

    def _apply_to_all(self) -> None:
        page = self.current()
        if not page or len(self.pages) < 2:
            return
        self._snapshot()
        for row, other in enumerate(self.pages):
            self.pages[row] = replace(other, crop=page.crop, target=page.target, filter=page.filter,
                                      brightness=page.brightness, contrast=page.contrast)
            self._refresh_thumb(row)
        self._show_current(True)
        self._changed()
        self.message.emit("Réglages appliqués à toutes les pages. « Annuler » revient en arrière.", "ok")

    # --- Export ---------------------------------------------------------------------------

    def _default_path(self, extension: str) -> str:
        folder = Path(self.cfg["scan_dir"])
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder / f"document-{dt.datetime.now():%Y-%m-%d-%H%M%S}.{extension}")

    def _ocr_lang_list(self) -> str:
        preferred = [lang for lang in ("fra", "eng") if lang in self.languages] or self.languages
        return "+".join(preferred)

    def export(self, selection_only: bool) -> None:
        pages = [self.pages[row] for row in self._rows()] if selection_only else list(self.pages)
        if not pages:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Exporter en PDF", self._default_path("pdf"), "PDF (*.pdf)")
        if path:
            self.export_to(pages, Path(path if path.lower().endswith(".pdf") else path + ".pdf"))

    def _export_progress(self, done: int, total: int) -> None:
        if self._export_toast:
            self._export_toast.set_progress(done, total)
            self._export_toast.update_text(f"Export de la page {done} sur {total}…")

    def _file_actions(self, target: Path) -> list[tuple]:
        def attempt(opener, failure):
            if not opener(target):
                self.message.emit(failure, "error")

        return [("Afficher dans le dossier", lambda: attempt(desktop.reveal_file, "Impossible d'afficher le dossier.")),
                ("Ouvrir", lambda: attempt(desktop.open_file, "Impossible d'ouvrir le fichier."))]

    def export_to(self, pages: list[Page], target: Path) -> None:
        self.export_button.setEnabled(False)
        lang = self._ocr_lang_list() if self.ocr.isChecked() else ""
        quality, password = self.quality.currentText(), self.password.text()
        self._export_toast = self._task(f"Export de {len(pages)} page(s)…")
        progress = self.exportProgress.emit

        def finish(_result):
            toast, self._export_toast = self._export_toast, None
            self._refresh_controls()
            self._finish_task(toast, f"PDF enregistré : {target.name}", "ok", self._file_actions(target))

        def failed(text):
            toast, self._export_toast = self._export_toast, None
            self._refresh_controls()
            self._finish_task(toast, text, "error")

        run_async(self, editor.export_pdf, pages, target, lang, progress, quality, password, done=finish, failed=failed)

    def _export_image(self) -> None:
        page = self.current()
        if not page:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Exporter la page", self._default_path("png"), "PNG (*.png);;JPEG (*.jpg)")
        if path:
            target = Path(path)
            toast = self._task("Export de l'image…")
            run_async(self, editor.export_image, page, target,
                      done=lambda _r: self._finish_task(toast, f"Image enregistrée : {target.name}", "ok", self._file_actions(target)),
                      failed=lambda text: self._finish_task(toast, text, "error"))

    def _to_print(self) -> None:
        pages = list(self.pages)
        target = self.folder / f"document-{dt.datetime.now():%Y%m%d-%H%M%S}.pdf"
        toast = self._task("Préparation pour l'impression…")

        def done(_result):
            self._finish_task(toast, "Document prêt à imprimer.")
            self.sendToPrint.emit(str(target))

        run_async(self, editor.export_pdf, pages, target, "", done=done,
                  failed=lambda text: self._finish_task(toast, text, "error"))


def _downscaled(image: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Aperçu et vignette. reduce() est très rapide, y compris sur un scan à 1200 dpi."""
    work = image.convert("L") if image.mode == "1" else image
    work = work.convert("RGB") if work.mode not in ("RGB", "L") else work
    factor = max(1, max(work.size) // PREVIEW_SIDE)
    preview = work.reduce(factor) if factor > 1 else work.copy()
    thumb = preview.copy()
    thumb.thumbnail((320, 320), Image.Resampling.BILINEAR)
    return preview, thumb
