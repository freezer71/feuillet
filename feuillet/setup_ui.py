"""Assistant d'ajout : l'application trouve les appareils et les configure toute seule."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QInputDialog, QLineEdit, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,
)

from . import admin, config, discovery, printing
from .discovery import Found
from .widgets import Spinner, label, run_async


def admin_task(owner: QWidget, job, done, failed, password: str | None = None, attempts: int = 3) -> None:
    """Lance une action en administrateur, et demande le mot de passe seulement s'il le faut.

    Le mot de passe ne vit que le temps de cette action : il n'est jamais enregistré.
    """
    def guarded():
        try:
            return "ok", job(password)
        except admin.NeedPassword:
            return "password", None
        except admin.WrongPassword:
            return "wrong", None
        except admin.AdminCancelled:
            return "cancelled", None

    def ask(prompt: str) -> None:
        text, accepted = QInputDialog.getText(owner, "Autorisation", prompt, QLineEdit.EchoMode.Password)
        if accepted and text:
            admin_task(owner, job, done, failed, text, attempts - 1)
        else:
            failed("Opération annulée.")

    def finish(result) -> None:
        status, value = result
        if status == "ok":
            done(value)
        elif status == "cancelled":
            failed("Opération annulée.")
        elif status == "password":
            ask("Pour configurer le système d'impression, saisis ton mot de passe.\nIl n'est pas enregistré.")
        elif attempts > 1:
            ask("Mot de passe incorrect. Réessaie.")
        else:
            failed("Mot de passe incorrect.")

    run_async(owner, guarded, done=finish, failed=failed)


def selectable(item: Found, known_scanners: set[str]) -> bool:
    if item.update_uri:
        return True
    new_printer = item.can_print and not item.configured
    new_scanner = bool(item.scanner) and item.scanner["id"] not in known_scanners
    return new_printer or new_scanner


def apply_found(items: list[Found], cfg: dict, password: str | None = None) -> list[str]:
    """Configure les appareils choisis et renvoie ce qui a été fait. S'arrête à la première erreur."""
    done = []
    known = {scanner["id"] for scanner in cfg["scanners"]}
    existing = printing.list_queues()
    for item in items:
        if item.update_uri:
            printing.update_queue_uri(item.queue, item.uri, password)
            done.append(f"Adresse de {item.name} mise à jour")
        elif item.can_print and not item.configured:
            if item.method == "brother":
                cfg["brother_host"] = item.host
            else:
                name = discovery.queue_name(item.name)
                printing.add_queue(name, item.uri, "everywhere" if item.method == "everywhere" else item.driver, item.name, password)
                if not existing and not cfg.get("default_set"):
                    printing.set_default_queue(name)
                    cfg["default_set"] = True
            done.append(f"{item.name} ajoutée")
        if item.scanner and item.scanner["id"] not in known:
            cfg["scanners"].append(item.scanner)
            known.add(item.scanner["id"])
            cfg["scanner"] = cfg.get("scanner") or item.scanner["id"]
            done.append(f"Scanner de {item.name} ajouté" if item.can_print else f"{item.name} ajouté")
    return done


class FoundRow(QFrame):
    def __init__(self, item: Found, known_scanners: set[str]):
        super().__init__()
        self.item = item
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)
        self.check = QCheckBox()
        usable = selectable(item, known_scanners)
        self.check.setEnabled(usable)
        self.check.setChecked(usable)
        self.check.setAccessibleName(item.name)
        row.addWidget(self.check)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        texts.addWidget(label(item.name))
        where = f"{item.summary} · {item.host}" if item.host else item.summary
        if item.note:
            where += f" · {item.note}"
        elif not usable and item.can_print:
            where += " · Déjà configurée"
        texts.addWidget(label(where, "small"))
        row.addLayout(texts, 1)


class AddDeviceDialog(QDialog):
    def __init__(self, parent: QWidget, cfg: dict):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Ajouter un appareil")
        self.resize(560, 500)
        self.rows: list[FoundRow] = []
        self.changed = False
        self._search_token = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        root.addWidget(label("Ajouter une imprimante ou un scanner", "h1"))
        self.intro = label("", "muted")
        root.addWidget(self.intro)
        self.searching = QWidget()
        search_row = QHBoxLayout(self.searching)
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.addWidget(Spinner(16))
        self.search_text = label("Recherche sur le réseau et en USB…", "muted")
        search_row.addWidget(self.search_text, 1)
        root.addWidget(self.searching)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self.list_layout = QVBoxLayout(holder)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch(1)
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

        manual = QHBoxLayout()
        self.address = QLineEdit()
        self.address.setPlaceholderText("Pas dans la liste ? Son adresse, ex. 192.168.1.20")
        self.address.returnPressed.connect(self._add_address)
        self.address_button = QPushButton("Chercher")
        self.address_button.clicked.connect(self._add_address)
        manual.addWidget(self.address, 1)
        manual.addWidget(self.address_button)
        root.addLayout(manual)
        self.error = label("", "error")
        self.error.hide()
        root.addWidget(self.error)

        buttons = QHBoxLayout()
        self.again = QPushButton("Relancer la recherche")
        self.again.clicked.connect(self.search)
        self.close_button = QPushButton("Fermer")
        self.close_button.clicked.connect(self.accept)
        self.add_button = QPushButton("Ajouter")
        self.add_button.setObjectName("primary")
        self.add_button.clicked.connect(self._apply)
        buttons.addWidget(self.again)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.add_button)
        root.addLayout(buttons)
        self.search()

    def _known_scanners(self) -> set[str]:
        return {scanner["id"] for scanner in self.cfg["scanners"]}

    def _clear(self) -> None:
        for row in self.rows:
            self.list_layout.removeWidget(row)
            row.deleteLater()
        self.rows = []

    def _add_row(self, item: Found) -> None:
        row = FoundRow(item, self._known_scanners())
        row.check.toggled.connect(self._update_button)
        self.list_layout.insertWidget(self.list_layout.count() - 1, row)
        self.rows.append(row)

    def _update_button(self) -> None:
        count = sum(row.check.isChecked() for row in self.rows)
        self.add_button.setEnabled(count > 0)
        self.add_button.setText(f"Ajouter ({count})" if count else "Ajouter")

    def _show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def search(self) -> None:
        self._search_token += 1
        token = self._search_token
        self._clear()
        self._show_error("")
        self.searching.show()
        self.search_text.setText("Recherche sur le réseau et en USB…")
        self.intro.setText("Allume l'appareil et vérifie qu'il est sur le même réseau, ou branché en USB.")
        self.add_button.setEnabled(False)
        brother = self.cfg.get("brother_host", "")

        def job():
            if not printing.service_running():
                return None
            return discovery.discover(printing.list_queues(), brother)

        def done(items):
            if token != self._search_token:
                return
            if items is None:
                self._service_stopped()
                return
            self.searching.hide()
            for item in items:
                self._add_row(item)
            self._update_button()
            ready = sum(row.check.isChecked() for row in self.rows)
            if not items:
                self.intro.setText("Aucun appareil trouvé. Indique son adresse ci-dessous, ou relance la recherche.")
            elif ready:
                self.intro.setText("Voici ce qui a été trouvé. Clique sur Ajouter : le reste est automatique.")
            else:
                self.intro.setText("Tout ce qui a été trouvé est déjà configuré.")

        def failed(text):
            if token == self._search_token:
                self.searching.hide()
                self._show_error(text)

        run_async(self, job, done=done, failed=failed)

    def _service_stopped(self) -> None:
        self.search_text.setText("Activation du service d'impression…")
        self.intro.setText("Le service d'impression du système est arrêté. Il faut l'activer une fois.")

        def done(_result):
            self.search()

        def failed(text):
            self.searching.hide()
            self._show_error(f"Impossible d'activer le service d'impression : {text}")

        admin_task(self, printing.start_service, done, failed)

    def _add_address(self) -> None:
        address = self.address.text().strip()
        if not address:
            return
        self.address_button.setEnabled(False)
        self._show_error("")
        brother = self.cfg.get("brother_host", "")

        def job():
            item = discovery.inspect_address(address)
            if item:
                discovery.mark_configured([item], printing.list_queues(), brother)
            return item

        def done(item):
            self.address_button.setEnabled(True)
            if item is None:
                self._show_error(f"Aucune imprimante ne répond à l'adresse {address}.")
                return
            self.address.clear()
            self._add_row(item)
            self.searching.hide()
            self._update_button()

        def failed(text):
            self.address_button.setEnabled(True)
            self._show_error(text)

        run_async(self, job, done=done, failed=failed)

    def _apply(self) -> None:
        items = [row.item for row in self.rows if row.check.isChecked()]
        if not items:
            return
        self._search_token += 1
        self.add_button.setEnabled(False)
        self.again.setEnabled(False)
        self.close_button.setEnabled(False)
        self._show_error("")
        self.searching.show()
        self.search_text.setText("Configuration en cours…")
        cfg = self.cfg

        def job(password):
            return apply_found(items, cfg, password)

        def done(results):
            config.save(cfg)
            self.changed = True
            self.searching.hide()
            self.close_button.setEnabled(True)
            self.again.setEnabled(True)
            self.intro.setText("C'est prêt : " + " ; ".join(results) + "." if results else "Rien à faire.")
            for row in self.rows:
                row.check.setChecked(False)
                row.check.setEnabled(False)

        def failed(text):
            config.save(cfg)    # ce qui a réussi avant l'erreur reste enregistré
            self.searching.hide()
            self.close_button.setEnabled(True)
            self.again.setEnabled(True)
            self._update_button()
            self._show_error(text)

        admin_task(self, job, done, failed)
