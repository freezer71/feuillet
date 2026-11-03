"""Trouve les imprimantes et les scanners, et décide comment les configurer."""
from __future__ import annotations

import re
import shutil
import ssl
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import devices

IPP_PATHS = ("ipp/print", "ipp/printer", "ipp", "printers/ipp", "")
PRINT_PORTS = (631, 9100, 515)
SNMP_MODEL = "1.3.6.1.2.1.25.3.2.1.3.1"


@dataclass
class Found:
    name: str
    host: str = ""
    uri: str = ""
    method: str = "unsupported"     # everywhere, driver, brother, scanner, unsupported
    driver: str = ""
    scanner: dict | None = None
    queue: str = ""                 # file CUPS déjà configurée pour cet appareil
    update_uri: bool = False        # la file existe, mais l'adresse a changé
    note: str = ""
    configured: bool = False

    @property
    def can_print(self) -> bool:
        return self.method in ("everywhere", "driver", "brother")

    @property
    def summary(self) -> str:
        parts = []
        if self.can_print:
            parts.append("Étiquettes" if self.method == "brother" else "Impression")
        if self.scanner:
            parts.append("Scan")
        return " et ".join(parts) if parts else "Non pris en charge"


def split_address(text: str) -> tuple[str, int | None, str | None]:
    """Accepte « 192.168.0.20 », « hôte:8631 » ou une adresse ipp:// complète."""
    text = text.strip()
    if "://" in text:
        parsed = urllib.parse.urlparse(text)
        return parsed.hostname or "", parsed.port, parsed.path.lstrip("/") or None
    host, _, port = text.partition(":")
    return host, int(port) if port.isdigit() else None, None


def format_host(host: str, port: int | None) -> str:
    return f"{host}:{port}" if port and port != 631 else host


def queue_name(model: str) -> str:
    """Un nom de file valide pour CUPS : ni espace, ni /, ni #."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", model).strip("_")
    return name[:60] or "Imprimante"


def host_of_uri(uri: str) -> str:
    return urllib.parse.urlparse(uri).hostname or ""


def _ipp_probe(host: str, port: int | None, path: str | None) -> tuple[str, dict] | None:
    for candidate in ([path] if path is not None else IPP_PATHS):
        uri = f"ipp://{host}:{port or 631}/{candidate}"
        attributes = devices.ipp_attributes(uri)
        if "printer-state" in attributes:
            return uri, attributes
    return None


def supports_driverless(attributes: dict) -> bool:
    formats = attributes.get("document-format-supported", "").lower()
    features = attributes.get("ipp-features-supported", "").lower()
    return "pwg-raster" in formats or "image/urf" in formats or "airprint" in features or "ipp-everywhere" in features


def find_driver(model: str) -> str:
    """Cherche un pilote installé pour ce modèle. Vide s'il n'y en a pas."""
    if not model:
        return ""
    try:
        result = subprocess.run(["lpinfo", "--make-and-model", model, "-m"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    words = [word.lower() for word in re.findall(r"[A-Za-z0-9-]{2,}", model) if word.lower() != "series"]
    for line in result.stdout.splitlines():
        driver, _, description = line.partition(" ")
        if driver and words and all(word in description.lower() for word in words[:3]):
            return driver
    return ""


def _is_brother_label(host: str) -> str:
    try:
        with urllib.request.urlopen(f"http://{host}/printer/main.html", timeout=8) as response:
            match = re.search(rb"Brother (QL-1060N)", response.read(4096))
            return f"Brother {match[1].decode()}" if match else ""
    except OSError:
        return ""


def _snmp_model(host: str) -> str:
    if not shutil.which("snmpget"):
        return ""
    try:
        result = subprocess.run(["snmpget", "-v1", "-c", "public", "-t", "2", "-r", "0", "-Ovq", host, SNMP_MODEL],
                                capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip().strip('"') if result.returncode == 0 else ""


def has_escl(host: str) -> bool:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    for scheme in ("https", "http"):
        try:
            with urllib.request.urlopen(f"{scheme}://{host}/eSCL/ScannerCapabilities", context=context if scheme == "https" else None, timeout=4) as response:
                if b"ScannerCapabilities" in response.read(2048):
                    return True
        except OSError:
            continue
    return False


def list_sane_devices() -> list[tuple[str, str]]:
    """Les scanners vus par SANE : (identifiant, description). Sans les webcams."""
    if not shutil.which("scanimage"):
        return []
    try:
        result = subprocess.run(["scanimage", "-L"], capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for match in re.finditer(r"device `([^']+)' is an? (.+)", result.stdout):
        if not match[1].startswith(("v4l:", "test:")):
            found.append((match[1], match[2].strip()))
    return found


def scanner_for_host(host: str, name: str, sane_devices: list[tuple[str, str]]) -> dict | None:
    """Le pilote du fabricant s'il est là, sinon eSCL, qui ne demande aucune installation."""
    for device, _description in sane_devices:
        if re.search(rf"(^|[^0-9.]){re.escape(host)}($|[^0-9.])", device):
            return {"id": device, "name": name, "backend": "sane", "device": device, "host": host}
    if has_escl(host):
        return {"id": f"escl:{host}", "name": name, "backend": "escl", "device": "", "host": host}
    return None


def inspect_address(address: str, sane_devices: list[tuple[str, str]] | None = None) -> Found | None:
    """Identifie un appareil à partir de son adresse."""
    host, port, path = split_address(address)
    if not host:
        return None
    sane_devices = list_sane_devices() if sane_devices is None else sane_devices
    target = format_host(host, port)
    probe = _ipp_probe(host, port, path)
    if probe:
        uri, attributes = probe
        model = attributes.get("printer-make-and-model") or attributes.get("printer-info") or target
        found = Found(name=model, host=target, uri=uri, scanner=scanner_for_host(host, model, sane_devices))
        if supports_driverless(attributes):
            found.method = "everywhere"
        else:
            found.driver = find_driver(model)
            found.method = "driver" if found.driver else "unsupported"
            if not found.driver:
                found.note = "Aucun pilote installé pour ce modèle."
        return found
    label = _is_brother_label(host)
    if label:
        return Found(name=label, host=host, method="brother")
    if not any(devices.port_open(host, candidate) for candidate in (9100, 515)):
        return None
    model = _snmp_model(host)
    found = Found(name=model or f"Imprimante {host}", host=host, uri=f"socket://{host}:9100")
    found.driver = find_driver(model)
    found.method = "driver" if found.driver else "unsupported"
    if not found.driver:
        found.note = "Modèle non identifié, ou aucun pilote installé."
    return found


def usb_printers() -> list[Found]:
    try:
        result = subprocess.run(["lpinfo", "--include-schemes", "usb", "-v"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in result.stdout.splitlines():
        match = re.match(r"direct (usb://\S+)", line)
        if match:
            model = urllib.parse.unquote(match[1].split("://", 1)[1].split("?", 1)[0]).replace("/", " ")
            item = Found(name=model, uri=match[1], driver=find_driver(model))
            item.method = "driver" if item.driver else "unsupported"
            if not item.driver:
                item.note = "Aucun pilote installé pour ce modèle."
            found.append(item)
    return found


def _alive(host: str) -> bool:
    return any(devices.port_open(host, port) for port in PRINT_PORTS)


def mark_configured(items: list[Found], queues: dict, brother_host: str) -> None:
    """Relie ce qui a été trouvé aux files existantes, et repère un changement d'adresse."""
    for item in items:
        if item.method == "brother":
            item.configured = item.host == brother_host
            continue
        same_host = [name for name, info in queues.items()
                     if item.uri and (info.get("device-uri") == item.uri or (item.host and host_of_uri(info.get("device-uri", "")) == item.host.split(":")[0]))]
        if same_host:
            item.queue, item.configured = same_host[0], True
            continue
        # Même modèle, mais une autre adresse : l'imprimante a sans doute changé d'IP.
        for name, info in queues.items():
            old_host = host_of_uri(info.get("device-uri", ""))
            model = info.get("printer-make-and-model", "")
            if old_host and not _alive(old_host) and (item.name.lower() in model.lower() or queue_name(item.name) == name):
                item.queue, item.update_uri = name, True
                item.note = f"Elle est maintenant à l'adresse {item.host}."
                break


def discover(queues: dict, brother_host: str = "") -> list[Found]:
    """Cherche sur le réseau local et en USB."""
    sane_devices = list_sane_devices()
    prefix = devices.local_prefix()
    items: list[Found] = []
    if prefix:
        hosts = [f"{prefix}.{index}" for index in range(1, 255)]
        with ThreadPoolExecutor(64) as pool:
            candidates = [host for host, alive in zip(hosts, pool.map(_alive, hosts)) if alive]
            items = [item for item in pool.map(lambda host: inspect_address(host, sane_devices), candidates) if item]
    items.extend(usb_printers())
    known = {item.scanner["id"] for item in items if item.scanner}
    for device, description in sane_devices:
        if device not in known and not re.search(r"\d+\.\d+\.\d+\.\d+", device):
            items.append(Found(name=description, method="scanner", scanner={
                "id": device, "name": description, "backend": "sane", "device": device, "host": ""}))
    mark_configured(items, queues, brother_host)
    return items
