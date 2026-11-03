"""Ouvre un fichier ou le montre dans le gestionnaire de fichiers du bureau."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtDBus import QDBusConnection, QDBusMessage
from PySide6.QtGui import QDesktopServices


def open_file(path: Path) -> bool:
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def _show_selected(path: Path) -> bool:
    """Demande au gestionnaire de fichiers d'ouvrir le dossier avec le fichier sélectionné."""
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return False
    message = QDBusMessage.createMethodCall("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                                            "org.freedesktop.FileManager1", "ShowItems")
    message.setArguments([[QUrl.fromLocalFile(str(path)).toString()], ""])
    return bus.call(message, timeout=3000).type() != QDBusMessage.MessageType.ErrorMessage


def reveal_file(path: Path) -> bool:
    """Montre le fichier dans son dossier. Sans ce service, ouvre simplement le dossier."""
    return _show_selected(path) or QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
