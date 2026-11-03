"""Scanner réseau sans pilote, via eSCL (AirScan)."""
from __future__ import annotations

import http.client
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NS = {"scan": "http://schemas.hp.com/imaging/escl/2011/05/03", "pwg": "http://www.pwg.org/schemas/2010/12/sm"}
READ_TIMEOUT = 40.0
# Sans image après ce délai, le scan est considéré comme bloqué et annulé.
STALL_TIMEOUT = 180.0


class ScanError(RuntimeError):
    pass


@dataclass
class Capabilities:
    model: str = ""
    max_size: tuple[int, int] = (2550, 3510)
    resolutions: list[int] = field(default_factory=lambda: [100, 200, 300, 600])


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
    return caps


def settings_xml(resolution: int, color: str, area: tuple[int, int]) -> bytes:
    width, height = area
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="{NS['scan']}" xmlns:pwg="{NS['pwg']}">
  <pwg:Version>2.0</pwg:Version>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
      <pwg:Width>{width}</pwg:Width>
      <pwg:Height>{height}</pwg:Height>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <pwg:InputSource>Platen</pwg:InputSource>
  <scan:ColorMode>{color}</scan:ColorMode>
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
  <scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>
  <scan:XResolution>{resolution}</scan:XResolution>
  <scan:YResolution>{resolution}</scan:YResolution>
</scan:ScanSettings>""".encode()


def retry_delay(header: str | None) -> float:
    """Respecte le délai demandé par le scanner, sans attendre trop longtemps ni trop peu."""
    try:
        return min(20.0, max(2.0, float(header)))
    except (TypeError, ValueError):
        return 3.0


class Scanner:
    """Le certificat d'une imprimante est auto-signé : sa vérification est désactivée, sur le réseau local."""

    def __init__(self, host: str, scheme: str = ""):
        self.host = host
        self.scheme = scheme
        self._context = ssl.create_default_context()
        self._context.check_hostname = False
        self._context.verify_mode = ssl.CERT_NONE

    def _open(self, url: str, data: bytes | None = None, method: str = "GET", timeout: float = 15.0):
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "text/xml")
        context = self._context if url.startswith("https") else None
        return urllib.request.urlopen(request, context=context, timeout=timeout)

    def base(self) -> str:
        """Certains appareils ne répondent qu'en HTTPS, d'autres qu'en HTTP."""
        if not self.scheme:
            for scheme in ("https", "http"):
                try:
                    self._open(f"{scheme}://{self.host}/eSCL/ScannerCapabilities", timeout=6).close()
                    self.scheme = scheme
                    break
                except (OSError, http.client.HTTPException):
                    continue
            else:
                raise ScanError("Scanner injoignable.")
        return f"{self.scheme}://{self.host}/eSCL"

    def _read(self, url: str, attempts: int = 3) -> bytes:
        # Le serveur web d'une imprimante coupe parfois la connexion sans répondre.
        for attempt in range(attempts):
            try:
                with self._open(url) as response:
                    return response.read()
            except urllib.error.HTTPError:
                raise
            except (OSError, http.client.HTTPException):
                if attempt == attempts - 1:
                    raise
                time.sleep(2)
        raise AssertionError

    def capabilities(self) -> Capabilities:
        return parse_capabilities(self._read(f"{self.base()}/ScannerCapabilities"))

    def state(self) -> str:
        return ET.fromstring(self._read(f"{self.base()}/ScannerStatus")).findtext("pwg:State", "", NS)

    def _cancel(self, job: str) -> None:
        try:
            self._open(job, method="DELETE", timeout=8).close()
        except (OSError, http.client.HTTPException):
            pass

    def scan(self, resolution: int, color: str, area: tuple[int, int] | None) -> bytes:
        """Lance un scan à la vitre et renvoie le JPEG reçu."""
        base = self.base()
        caps = self.capabilities()
        width, height = area or caps.max_size
        region = (min(width, caps.max_size[0]), min(height, caps.max_size[1]))
        try:
            with self._open(f"{base}/ScanJobs", settings_xml(resolution, color, region), "POST", timeout=30) as response:
                location = response.headers.get("Location", "")
        except urllib.error.HTTPError as error:
            if error.code == 503:
                raise ScanError("Le scanner est occupé. Regarde si un message s'affiche sur l'écran de "
                                "l'imprimante, ferme-le, puis réessaie.") from error
            raise ScanError(f"Le scanner a refusé la demande (HTTP {error.code}).") from error
        except (OSError, http.client.HTTPException) as error:
            raise ScanError(f"Scanner injoignable : {error}") from error
        if not location:
            raise ScanError("Le scanner n'a pas indiqué de travail à suivre.")
        job = urllib.parse.urljoin(base + "/", location).rstrip("/")

        started = time.monotonic()
        delivered = False
        try:
            while True:
                if time.monotonic() - started > STALL_TIMEOUT:
                    raise ScanError("Le scanner ne livre aucune image. Regarde si un message s'affiche sur "
                                    "l'écran de l'imprimante, ferme-le, puis réessaie.")
                try:
                    with self._open(f"{job}/NextDocument", timeout=READ_TIMEOUT) as response:
                        data = response.read()
                    delivered = True
                    return data
                except urllib.error.HTTPError as error:
                    if error.code != 503:
                        raise ScanError(f"Le scan a échoué (HTTP {error.code}).") from error
                    time.sleep(retry_delay(error.headers.get("Retry-After")))
                except (OSError, http.client.HTTPException):
                    time.sleep(5)
        finally:
            if not delivered:
                self._cancel(job)
