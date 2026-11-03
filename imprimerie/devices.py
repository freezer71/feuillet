"""État des appareils et recherche sur le réseau local."""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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


def parse_ipp(output: str) -> dict[str, str]:
    values = {}
    for line in output.splitlines():
        match = re.match(r"\s*([a-z0-9-]+) \([^)]*\) = (.*)$", line)
        if match:
            values[match[1]] = match[2].strip()
    return values


def ipp_status(host: str) -> Status:
    try:
        result = subprocess.run(
            ["ipptool", "-tv", "-T", "5", f"ipp://{host}:631/ipp/print", "get-printer-attributes.test"],
            capture_output=True, text=True, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        return Status()
    attrs = parse_ipp(result.stdout)
    if "printer-state" not in attrs:
        return Status()
    status = Status(online=True, model=attrs.get("printer-make-and-model", ""),
                    state=STATES.get(attrs["printer-state"], attrs["printer-state"]))
    reasons = attrs.get("printer-state-reasons", "none")
    status.detail = "" if reasons == "none" else reasons
    names = attrs.get("marker-names", "").split(",")
    colors = attrs.get("marker-colors", "").split(",")
    levels = attrs.get("marker-levels", "").split(",")
    for name, color, level in zip(names, colors, levels):
        if level.strip().lstrip("-").isdigit():
            status.ink.append((INK_NAMES.get(name, name), color, int(level)))
    return status


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


def _port_open(host: str, port: int) -> bool:
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


def identify(host: str) -> str | None:
    """Renvoie 'epson', 'brother' ou None."""
    if ipp_status(host).model.upper().startswith("EPSON"):
        return "epson"
    try:
        with urllib.request.urlopen(f"http://{host}/printer/main.html", timeout=3) as response:
            if b"Brother QL" in response.read(4096):
                return "brother"
    except OSError:
        pass
    return None


def discover() -> dict[str, str]:
    """Cherche l'Epson et la Brother sur le /24 local : {'epson': ip, 'brother': ip}."""
    prefix = local_prefix()
    if not prefix:
        return {}
    hosts = [f"{prefix}.{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(64) as pool:
        candidates = [h for h, up in zip(hosts, pool.map(lambda h: _port_open(h, 9100), hosts)) if up]
        found = {}
        for host, kind in zip(candidates, pool.map(identify, candidates)):
            if kind:
                found.setdefault(kind, host)
    return found
