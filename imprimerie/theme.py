"""Couleurs et feuille de style de l'application. Toute couleur passe par ces jetons."""
from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).parent / "assets"

# Sobriété : des gris neutres, sans couleur d'accent. La seule couleur vive, "mark", sert à voir
# le cadre et la sélection de texte sur une page blanche. Les états restent discrets.
PALETTES = {
    "sombre": {
        "bg": "#1E1E1F", "nav": "#1E1E1F", "surface": "#1E1E1F", "raised": "#2A2A2C", "hover": "#2F2F31",
        "field": "#252527", "border": "#333335", "text": "#DEDEE0", "muted": "#9A9A9C", "faint": "#6A6A6C",
        "primary": "#DEDEE0", "primary_hover": "#F0F0F1", "on_primary": "#1E1E1F", "selected": "#333335",
        "mark": "#4C7DB8", "ok": "#8FB39A", "warn": "#C9A560", "error": "#D27C7C", "canvas": "#171718", "shade": "#A0101012",
    },
    "clair": {
        "bg": "#F7F7F7", "nav": "#F7F7F7", "surface": "#F7F7F7", "raised": "#ECECEC", "hover": "#E6E6E6",
        "field": "#FFFFFF", "border": "#DCDCDC", "text": "#222224", "muted": "#6B6B6D", "faint": "#9C9C9E",
        "primary": "#2A2A2C", "primary_hover": "#000000", "on_primary": "#FFFFFF", "selected": "#E2E2E2",
        "mark": "#2F6DB0", "ok": "#4B7A5B", "warn": "#8F6B1E", "error": "#B04A4A", "canvas": "#E4E4E4", "shade": "#80222224",
    },
}

_current = "sombre"


def set_theme(name: str) -> None:
    global _current
    _current = name if name in PALETTES else "sombre"


def current_name() -> str:
    return _current


def color(token: str) -> str:
    return PALETTES[_current][token]


def build_qss() -> str:
    p = PALETTES[_current]
    variant = "dark" if _current == "sombre" else "light"
    arrow, check = (ASSETS / f"arrow-{variant}.svg").as_posix(), (ASSETS / f"check-{variant}.svg").as_posix()
    return f"""
* {{ font-family: "Noto Sans", sans-serif; font-size: 13px; }}
QMainWindow, QDialog, #page {{ background: {p['bg']}; }}
QWidget {{ color: {p['text']}; }}
QToolTip {{ background: {p['raised']}; color: {p['text']}; border: 1px solid {p['border']}; padding: 3px; }}
#nav {{ background: {p['nav']}; border-right: 1px solid {p['border']}; }}
#brand {{ font-size: 13px; font-weight: 600; color: {p['muted']}; }}
#navButton {{ background: transparent; border: none; border-radius: 4px; text-align: left; padding: 7px 10px; color: {p['muted']}; }}
#navButton:hover {{ color: {p['text']}; }}
#navButton:checked {{ background: {p['selected']}; color: {p['text']}; }}
#panel {{ background: transparent; border: none; }}
#toolbar {{ background: transparent; border: none; }}
#title {{ font-size: 13px; font-weight: 600; }}
#h1 {{ font-size: 15px; font-weight: 600; }}
#sectionHeader {{ background: transparent; border: none; text-align: left; font-weight: 600; padding: 4px 0; color: {p['text']}; }}
#sectionHeader:hover {{ background: transparent; }}
#muted {{ color: {p['muted']}; }}
#small {{ color: {p['muted']}; font-size: 12px; }}
#warn {{ color: {p['warn']}; }}
#error {{ color: {p['error']}; }}
#ok {{ color: {p['muted']}; }}
#preview {{ background: {p['canvas']}; border: none; }}
#toast {{ background: {p['raised']}; border: 1px solid {p['border']}; border-radius: 4px; }}
QPushButton {{ background: transparent; border: 1px solid {p['border']}; border-radius: 4px; padding: 5px 10px; min-height: 18px; }}
QPushButton:hover {{ background: {p['hover']}; }}
QPushButton:focus {{ border-color: {p['faint']}; }}
QPushButton:disabled {{ color: {p['faint']}; }}
QPushButton:checked {{ background: {p['selected']}; }}
QPushButton#primary {{ background: {p['primary']}; border: none; color: {p['on_primary']}; font-weight: 600; padding: 7px 12px; }}
QPushButton#primary:hover {{ background: {p['primary_hover']}; }}
QPushButton#primary:disabled {{ background: {p['raised']}; color: {p['faint']}; }}
QPushButton#tool {{ background: transparent; border: none; padding: 5px 8px; }}
QPushButton#tool:hover {{ background: {p['hover']}; }}
QPushButton#tool:checked {{ background: {p['selected']}; }}
QPushButton#tool:disabled {{ background: transparent; }}
QPushButton#link {{ background: transparent; border: none; color: {p['text']}; padding: 0 4px; text-decoration: underline; }}
QComboBox, QLineEdit, QSpinBox {{ background: {p['field']}; border: 1px solid {p['border']}; border-radius: 4px;
  padding: 4px 6px; min-height: 18px; selection-background-color: {p['mark']}; selection-color: white; }}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus {{ border-color: {p['faint']}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{ image: url("{arrow}"); width: 12px; height: 12px; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; background: transparent; width: 16px; }}
QComboBox QAbstractItemView {{ background: {p['field']}; border: 1px solid {p['border']}; selection-background-color: {p['selected']}; outline: 0; }}
QListWidget, QTableWidget {{ background: transparent; border: none; gridline-color: {p['border']}; outline: 0; }}
QListWidget::item {{ padding: 5px; border-radius: 3px; }}
QListWidget::item:hover {{ background: {p['hover']}; }}
QListWidget::item:selected {{ background: {p['selected']}; color: {p['text']}; }}
#slides::item {{ padding: 6px; margin: 1px; color: {p['muted']}; }}
#slides::item:selected {{ color: {p['text']}; }}
QHeaderView::section {{ background: transparent; border: none; border-bottom: 1px solid {p['border']}; padding: 6px; color: {p['muted']}; font-weight: 400; }}
QTableWidget::item {{ padding: 2px 6px; border-bottom: 1px solid {p['border']}; }}
QTableWidget::item:selected {{ background: {p['selected']}; color: {p['text']}; }}
QProgressBar {{ background: {p['border']}; border: none; border-radius: 1px; max-height: 3px; min-height: 3px; }}
QProgressBar::chunk {{ background: {p['muted']}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 1px; }}
QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {p['faint']}; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 1px; }}
QScrollBar::handle:horizontal {{ background: {p['border']}; border-radius: 3px; min-width: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QSlider::groove:horizontal {{ background: {p['border']}; height: 2px; }}
QSlider::sub-page:horizontal {{ background: {p['muted']}; }}
QSlider::handle:horizontal {{ background: {p['text']}; width: 10px; height: 10px; margin: -4px 0; border-radius: 5px; }}
QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 3px; border: 1px solid {p['faint']}; background: {p['field']}; }}
QCheckBox::indicator:checked {{ background: {p['primary']}; border-color: {p['primary']}; image: url("{check}"); }}
QCheckBox:disabled {{ color: {p['faint']}; }}
"""
