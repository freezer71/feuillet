"""Lecture de l'état du scanner via eSCL.

Aucun scan ne passe par ici : sur la XP-6000, un travail eSCL peut rester bloqué
jusqu'au redémarrage de l'imprimante. Les scans utilisent sane_scan.py.
"""
from __future__ import annotations

import http.client
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NS = {"scan": "http://schemas.hp.com/imaging/escl/2011/05/03", "pwg": "http://www.pwg.org/schemas/2010/12/sm"}

class ScanError(RuntimeError):
    pass


@dataclass
class Capabilities:
    model: str = ""
    max_size: tuple[int, int] = (2550, 3510)
    resolutions: list[int] = field(default_factory=lambda: [100, 200, 300, 600])
    intents: list[str] = field(default_factory=list)


def parse_capabilities(xml: bytes) -> Capabilities:
    root = ET.fromstring(xml)
    caps = Capabilities()
    caps.model = root.findtext("pwg:MakeAndModel", "", NS)
    platen = root.find(".//scan:PlatenInputCaps", NS)
    if platen is not None:
        caps.max_size = (int(platen.findtext("scan:MaxWidth", "2550", NS)),
                         int(platen.findtext("scan:MaxHeight", "3510", NS)))
        values = {int(node.text) for node in platen.findall(".//scan:DiscreteResolution/scan:XResolution", NS)}
        if values:
            caps.resolutions = sorted(values)
        caps.intents = [node.text for node in platen.findall(".//scan:Intent", NS)]
    return caps


class Scanner:
    """Le certificat d'une imprimante est auto-signé : sa vérification est désactivée, sur le réseau local."""

    def __init__(self, host: str):
        self.base = f"https://{host}/eSCL"
        self._context = ssl.create_default_context()
        self._context.check_hostname = False
        self._context.verify_mode = ssl.CERT_NONE

    def _read(self, url: str, attempts: int = 3) -> bytes:
        # Le serveur web de l'imprimante coupe parfois la connexion sans répondre.
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(url, context=self._context, timeout=10) as response:
                    return response.read()
            except urllib.error.HTTPError:
                raise
            except (OSError, http.client.HTTPException):
                if attempt == attempts - 1:
                    raise
                time.sleep(2)
        raise AssertionError

    def capabilities(self) -> Capabilities:
        return parse_capabilities(self._read(f"{self.base}/ScannerCapabilities"))

    def state(self) -> str:
        return ET.fromstring(self._read(f"{self.base}/ScannerStatus")).findtext("pwg:State", "", NS)
