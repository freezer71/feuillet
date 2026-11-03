"""État des appareils et recherche sur le réseau local."""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field

SNMP_STATE = "1.3.6.1.2.1.25.3.5.1.1.1"
SNMP_MESSAGE = "1.3.6.1.2.1.43.16.5.1.2.1.1"
SNMP_MEDIA = "1.3.6.1.2.1.43.8.2.1.12.1.1"


@dataclass
class Status:
    online: bool = False
    state: str = "Injoignable"
    detail: str = ""
    model: str = ""
    ink: list[tuple[str, str, int]] = field(default_factory=list)   # nom, couleur, %
    media: str = ""


INK_NAMES = {"Photo Black ink": "Noir photo", "Black ink": "Noir", "Cyan ink": "Cyan",
             "Magenta ink": "Magenta", "Yellow ink": "Jaune"}
STATES = {"idle": "Prête", "processing": "En cours d'impression", "stopped": "Arrêtée"}
REASONS = {"media-empty": "plus de papier", "media-needed": "plus de papier", "media-jam": "bourrage papier",
           "toner-low": "toner bientôt vide", "toner-empty": "toner vide", "marker-supply-low": "encre bientôt vide",
           "marker-supply-empty": "plus d'encre", "door-open": "capot ouvert", "cover-open": "capot ouvert",
           "offline": "hors ligne", "paused": "en pause"}


def parse_ipp(output: str) -> dict[str, str]:
    values = {}
    for line in output.splitlines():
        match = re.match(r"\s*([a-z0-9-]+) \([^)]*\) = (.*)$", line)
        if match:
            values[match[1]] = match[2].strip()
    return values


def ipp_attributes(uri: str) -> dict[str, str]:
    try:
        result = subprocess.run(["ipptool", "-tv", "-T", "5", uri, "get-printer-attributes.test"],
                                capture_output=True, text=True, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_ipp(result.stdout)


def status_from_attributes(attrs: dict[str, str]) -> Status:
    if "printer-state" not in attrs:
        return Status()
    status = Status(online=True, model=attrs.get("printer-make-and-model", ""),
                    state=STATES.get(attrs["printer-state"], attrs["printer-state"]))
    reasons = attrs.get("printer-state-reasons", "none")
    status.detail = "" if reasons == "none" else REASONS.get(reasons.split(",")[0].rsplit("-", 1)[0] if reasons.split(",")[0].endswith(("-report", "-warning", "-error")) else reasons.split(",")[0], reasons)
    names = attrs.get("marker-names", "").split(",")
    colors = attrs.get("marker-colors", "").split(",")
    levels = attrs.get("marker-levels", "").split(",")
    for name, color, level in zip(names, colors, levels):
        if level.strip().lstrip("-").isdigit():
            status.ink.append((INK_NAMES.get(name, name), color, int(level)))
    return status


def queue_status(uri: str) -> Status:
    """État d'une imprimante à partir de l'adresse de sa file. Seul IPP donne le détail."""
    if uri.startswith(("ipp://", "ipps://")):
        return status_from_attributes(ipp_attributes(uri))
    return Status(online=True, state="Prête")


def _snmp(host: str, oid: str) -> str:
    result = subprocess.run(["snmpget", "-v1", "-c", "public", "-t", "2", "-r", "0", "-Ovq", host, oid],
                            capture_output=True, text=True, timeout=6)
    if result.returncode != 0:
        return ""
    return result.stdout.strip().replace('\\"', "\u2033").strip('"').replace("\u2033", '"').strip()


def brother_status(host: str) -> Status:
    try:
        with socket.create_connection((host, 9100), timeout=3):
            pass
    except OSError:
        return Status()
    status = Status(online=True, state="Prête", model="Brother QL-1060N")
    if not shutil.which("snmpget"):
        status.detail = "État détaillé indisponible : snmpget n'est pas installé."
        return status
    try:
        message = _snmp(host, SNMP_MESSAGE)
        status.media = _snmp(host, SNMP_MEDIA)
        state = _snmp(host, SNMP_STATE)
    except (OSError, subprocess.TimeoutExpired):
        return status
    if state and state != "idle":
        status.state = STATES.get(state, state)
    if message and message.upper() != "READY":
        status.state = "À vérifier"
        status.detail = message
    return status


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def local_prefix() -> str | None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return address.rsplit(".", 1)[0]
