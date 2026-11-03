"""Éléments partagés par les écrans : travail en arrière-plan et petits composants."""
from __future__ import annotations

import traceback

from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QEvent, QObject, QPropertyAnimation, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from . import icons, theme

class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args, self.signals = fn, args, _Signals()

    def run(self):
        try:
            result = self.fn(*self.args)
        except Exception as error:  # noqa: BLE001 - toute erreur doit remonter à l'utilisateur
            traceback.print_exc()
            try:
                self.signals.failed.emit(str(error) or error.__class__.__name__)
            except RuntimeError:
                pass
        else:
            try:
                self.signals.done.emit(result)
            except RuntimeError:
                pass


def run_async(owner: QObject, fn, *args, done=None, failed=None) -> None:
    worker = Worker(fn, *args)
    worker.setAutoDelete(False)
    jobs = owner.__dict__.setdefault("_workers", set())
    jobs.add(worker)

    def finish(callback, value):
        jobs.discard(worker)
        if callback:
            callback(value)

    worker.signals.done.connect(lambda value: finish(done, value))
    worker.signals.failed.connect(lambda message: finish(failed, message))
    QThreadPool.globalInstance().start(worker)


def to_pixmap(image: Image.Image) -> QPixmap:
    if image.mode not in ("RGB", "RGBA", "L"):
        image = image.convert("L")
    return QPixmap.fromImage(QImage(ImageQt(image)))


def label(text: str = "", name: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setWordWrap(True)
    if name:
        widget.setObjectName(name)
    return widget


def compact_combo() -> QComboBox:
    """Une liste qui s'adapte au panneau au lieu de l'élargir pour son texte le plus long."""
    box = QComboBox()
    box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(10)
    return box


def panel() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(8)
    return frame, layout


def set_kind(widget: QLabel, text: str, kind: str) -> None:
    widget.setObjectName(kind)
    widget.setText(text)
    widget.style().polish(widget)


class Spinner(QWidget):
    """Un petit arc qui tourne, pour les attentes sans durée connue."""

    def __init__(self, size: int = 18):
        super().__init__()
        self.setFixedSize(size, size)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        self._angle = (self._angle + 6) % 360
        self.update()

    def stop(self) -> None:
        self._timer.stop()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(theme.color("muted")), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(self.rect().adjusted(2, 2, -2, -2), -self._angle * 16, 270 * 16)


class Toast(QFrame):
    """Une notification. Une tâche affiche un loader, puis indique comment elle s'est terminée."""
    closed = Signal(object)

    def __init__(self, text: str, kind: str = "info", busy: bool = False):
        super().__init__()
        self.setObjectName("toast")
        self.setMinimumWidth(240)
        self.setMaximumWidth(360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.spinner = Spinner(14) if busy else None
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(18, 18)
        if self.spinner:
            row.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignTop)
            self.icon_label.hide()
        row.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        self.message = QLabel(text)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.message, 1)
        layout.addLayout(row)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        self.actions.addStretch(1)
        layout.addLayout(self.actions)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 0)
        self.bar.setVisible(busy)
        layout.addWidget(self.bar)
        self._fade = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._fade)
        self._animate(0.0, 1.0)
        self.finished = not busy
        if not busy:
            self._apply_kind(kind)
            self._close_later(kind)

    def _animate(self, start: float, end: float, then=None) -> None:
        animation = QPropertyAnimation(self._fade, b"opacity", self)
        animation.setDuration(180)
        animation.setStartValue(start)
        animation.setEndValue(end)
        if then:
            animation.finished.connect(then)
        animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _apply_kind(self, kind: str) -> None:
        names = {"warn": ("alert", "warn"), "error": ("alert", "error")}
        if kind in names:
            name, token = names[kind]
            self.icon_label.setPixmap(icons.pixmap(name, 16, token))
            self.icon_label.show()
        else:
            self.icon_label.hide()

    def _close_later(self, kind: str, has_actions: bool = False) -> None:
        # Un bouton doit laisser le temps de s'en servir.
        QTimer.singleShot(20000 if has_actions else 12000 if kind == "error" else 6000, self.dismiss)

    def add_actions(self, actions: list[tuple]) -> None:
        for text, handler in actions:
            button = QPushButton(text)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(handler)
            self.actions.addWidget(button)

    def update_text(self, text: str) -> None:
        self.message.setText(text)

    def set_progress(self, done: int, total: int) -> None:
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)

    def finish(self, text: str, kind: str = "ok", actions: list[tuple] | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        if self.spinner:
            self.spinner.stop()
            self.spinner.hide()
        self.bar.hide()
        self.message.setText(text)
        self._apply_kind(kind)
        if actions:
            self.add_actions(actions)
        self._close_later(kind, bool(actions))
        host = self.parentWidget()
        if isinstance(host, ToastHost):
            QTimer.singleShot(0, host.reposition)

    def dismiss(self) -> None:
        try:
            self._animate(1.0, 0.0, lambda: self.closed.emit(self))
        except RuntimeError:
            pass


class ToastHost(QWidget):
    """Empile les notifications en bas à droite de la fenêtre."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.column.setSpacing(8)
        parent.installEventFilter(self)
        self.hide()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self.reposition()
        return False

    def reposition(self) -> None:
        parent = self.parentWidget()
        self.setFixedWidth(max(240, min(360, parent.width() - 40)))
        self.adjustSize()
        self.move(parent.width() - self.width() - 20, parent.height() - self.height() - 20)
        self.raise_()

    def _add(self, toast: Toast) -> Toast:
        # Trois messages au plus : les plus anciens déjà terminés laissent la place.
        shown = [self.column.itemAt(i).widget() for i in range(self.column.count())]
        finished = [item for item in shown if item.finished]
        for old in finished[:max(0, len(shown) - 2)]:
            self._remove(old)
        self.column.addWidget(toast)
        toast.closed.connect(self._remove)
        self.show()
        self.reposition()
        QTimer.singleShot(0, self.reposition)
        return toast

    def _remove(self, toast: Toast) -> None:
        if self.column.indexOf(toast) < 0:
            return      # déjà retiré pour faire de la place, avant la fin de son fondu
        self.column.removeWidget(toast)
        toast.deleteLater()
        if self.column.count() == 0:
            self.hide()
        else:
            QTimer.singleShot(0, self.reposition)

    def show_message(self, text: str, kind: str = "ok") -> Toast:
        return self._add(Toast(text, kind))

    def task(self, text: str) -> Toast:
        return self._add(Toast(text, busy=True))


class Section(QWidget):
    """Un groupe d'options repliable."""

    def __init__(self, title: str, content: QWidget, opened: bool = True):
        super().__init__()
        self.content = content
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.header = QPushButton(title)
        self.header.setObjectName("sectionHeader")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.clicked.connect(self.toggle)
        layout.addWidget(self.header)
        layout.addWidget(content)
        self._set(opened)

    def _set(self, opened: bool) -> None:
        self.content.setVisible(opened)
        icons.set_icon(self.header, "chevron_down" if opened else "chevron_right", 16, "muted")

    def toggle(self) -> None:
        self._set(not self.content.isVisible())


def empty_state(painter: QPainter, rect, _icon_name: str, title: str, text: str) -> None:
    """Écran vide : deux lignes de texte, rien de plus."""
    box = rect.adjusted(60, rect.height() // 2 - 40, -60, 0)
    painter.setPen(QColor(theme.color("text")))
    painter.drawText(box, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, title)
    painter.setPen(QColor(theme.color("muted")))
    painter.drawText(box.adjusted(0, 26, 0, 0), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, text)


# Modes d'affichage selon la largeur de la fenêtre.
WIDE, MEDIUM, NARROW = "large", "moyen", "etroit"


def layout_mode(width: int) -> str:
    if width >= 1100:
        return WIDE
    return MEDIUM if width >= 760 else NARROW


class ViewSwitch(QWidget):
    """Sur une fenêtre étroite, alterne entre la page et ses réglages : ils n'y tiennent pas côte à côte."""
    changed = Signal(bool)      # vrai : les réglages sont affichés

    def __init__(self, main_label: str):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.main_button = QPushButton(main_label)
        self.settings_button = QPushButton("Réglages")
        for button in (self.main_button, self.settings_button):
            button.setCheckable(True)
            button.setAutoExclusive(True)
            row.addWidget(button, 1)
        self.main_button.setChecked(True)
        self.settings_button.toggled.connect(self.changed.emit)

    def showing_settings(self) -> bool:
        return self.settings_button.isChecked()
