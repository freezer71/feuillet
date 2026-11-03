"""Impression via CUPS : files disponibles, options, envoi et suivi des travaux."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

import cups

OPTION_LABELS = {
    "PageSize": "Format du papier",
    "MediaType": "Type de papier",
    "InputSlot": "Source du papier",
    "ColorModel": "Couleur",
    "Duplex": "Recto verso",
    "cupsPrintQuality": "Qualité",
}
VALUE_LABELS = {
    "RGB": "Couleur", "Gray": "Noir et blanc", "None": "Recto seul",
    "DuplexNoTumble": "Recto verso, bord long", "DuplexTumble": "Recto verso, bord court",
    "Draft": "Brouillon", "Normal": "Normale", "High": "Haute",
    "Auto": "Automatique", "Main": "Bac principal", "Photo": "Bac photo", "Disc": "CD / DVD",
    "Stationery": "Papier ordinaire", "Photographic": "Photo",
    "PhotographicHighGloss": "Photo très brillant", "PhotographicSemiGloss": "Photo satiné",
    "PhotographicGlossy": "Photo brillant", "PhotographicMatte": "Photo mat",
    "Letter": "Lettre US", "Legal": "Legal US", "Env10": "Enveloppe n° 10",
    "EnvC6": "Enveloppe C6", "EnvDL": "Enveloppe DL", "Postcard": "Carte postale",
}


def value_label(value: str) -> str:
    if value.endswith(".Borderless"):
        base = value[: -len(".Borderless")]
        return f"{VALUE_LABELS.get(base, base)} sans marges"
    return VALUE_LABELS.get(value, value)


@dataclass
class Option:
    key: str
    label: str
    values: list[str]
    default: str


def parse_options(text: str) -> list[Option]:
    options = []
    for line in text.splitlines():
        match = re.match(r"^([^/:]+)/[^:]*:\s*(.*)$", line)
        if not match or match[1] not in OPTION_LABELS:
            continue
        values, default = [], ""
        for token in match[2].split():
            if token.startswith("*"):
                token = token[1:]
                default = token
            if not token.startswith("Custom."):
                values.append(token)
        options.append(Option(match[1], OPTION_LABELS[match[1]], values, default))
    order = list(OPTION_LABELS)
    return sorted(options, key=lambda option: order.index(option.key))


def queue_options(queue: str) -> list[Option]:
    result = subprocess.run(["lpoptions", "-p", queue, "-l"], capture_output=True, text=True, timeout=10)
    return parse_options(result.stdout) if result.returncode == 0 else []


def list_queues() -> dict[str, dict]:
    return cups.Connection().getPrinters()


def default_queue() -> str | None:
    return cups.Connection().getDefault()


def is_raw_brother_queue(name: str, info: dict) -> bool:
    """Une file brute vers une QL ne sait rien convertir : l'application passe en direct."""
    return "QL" in name.upper() and info.get("printer-make-and-model", "").lower().startswith("local")


def valid_ranges(text: str) -> bool:
    return not text.strip() or bool(re.fullmatch(r"\s*\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*\s*", text))


def expand_ranges(text: str, count: int) -> list[int]:
    """'1-3,5' devient [0, 1, 2, 4], sans dépasser le nombre de pages."""
    if not text.strip():
        return list(range(count))
    if not valid_ranges(text):
        raise ValueError(f"Pages invalides : {text}")
    pages: list[int] = []
    for part in text.replace(" ", "").split(","):
        start, _, end = part.partition("-")
        for page in range(int(start), int(end or start) + 1):
            if 1 <= page <= count and page - 1 not in pages:
                pages.append(page - 1)
    return pages


def lp_args(queue: str, path: str, title: str, copies: int = 1, pages: str = "",
            options: dict[str, str] | None = None, fit: bool = False, orientation: str = "") -> list[str]:
    args = ["lp", "-d", queue, "-t", title, "-n", str(max(1, copies))]
    if pages.strip():
        args += ["-P", pages.replace(" ", "")]
    for key, value in (options or {}).items():
        args += ["-o", f"{key}={value}"]
    if fit:
        args += ["-o", "fit-to-page"]
    if orientation == "paysage":
        args += ["-o", "orientation-requested=4"]
    elif orientation == "portrait":
        args += ["-o", "orientation-requested=3"]
    return args + ["--", path]


def submit(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "L'impression a été refusée.")
    match = re.search(r"(\S+-\d+)", result.stdout)
    return match[1] if match else result.stdout.strip()


JOB_STATES = {3: "En attente", 4: "Suspendu", 5: "Impression", 6: "Arrêté", 7: "Annulé", 8: "Échec", 9: "Terminé"}


def list_jobs(limit: int = 50) -> list[dict]:
    jobs = cups.Connection().getJobs(which_jobs="all", my_jobs=False, limit=limit, requested_attributes=[
        "job-id", "job-name", "job-state", "job-printer-uri", "time-at-creation"])
    rows = []
    for job_id, job in sorted(jobs.items(), reverse=True):
        rows.append({
            "id": job_id,
            "name": job.get("job-name", ""),
            "printer": job.get("job-printer-uri", "").rsplit("/", 1)[-1],
            "state": JOB_STATES.get(job.get("job-state"), "?"),
            "active": job.get("job-state") in (3, 4, 5, 6),
            "time": job.get("time-at-creation", 0),
        })
    return rows


def job_state(job_id: int) -> int:
    return cups.Connection().getJobAttributes(job_id, requested_attributes=["job-state"])["job-state"]


def cancel_job(job_id: int) -> None:
    cups.Connection().cancelJob(job_id)
