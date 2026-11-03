"""Actions qui demandent les droits d'administrateur, sans terminal.

Un agent polkit affiche son propre dialogue quand il existe. Beaucoup de bureaux légers n'en ont
pas : l'application demande alors le mot de passe elle-même et le transmet à sudo, le temps d'une
commande. Il n'est ni enregistré ni journalisé.
"""
from __future__ import annotations

import os
import shutil
import subprocess


class NeedPassword(Exception):
    """Aucun agent ne peut demander l'autorisation : il faut le mot de passe."""


class WrongPassword(Exception):
    pass


class AdminCancelled(Exception):
    pass


class AdminError(RuntimeError):
    pass


def _environment() -> dict:
    # Les messages sont analysés ci-dessous : ils doivent rester en anglais.
    return {**os.environ, "LC_ALL": "C", "LANG": "C"}


def run_admin(args: list[str], password: str | None = None, timeout: float = 180) -> str:
    """Lance une commande en administrateur et renvoie sa sortie. Jamais de shell."""
    if os.geteuid() == 0:
        command, stdin = args, None
    elif password is not None:
        if not shutil.which("sudo"):
            raise AdminError("sudo n'est pas installé sur ce système.")
        command, stdin = ["sudo", "-S", "-k", "-p", "", "--", *args], password + "\n"
    elif shutil.which("pkexec"):
        command, stdin = ["pkexec", *args], None
    else:
        raise NeedPassword()
    try:
        # Une entrée vide : sans agent, pkexec ne doit surtout pas attendre une saisie au clavier.
        result = subprocess.run(command, input=stdin or "", capture_output=True, text=True, timeout=timeout, env=_environment())
    except subprocess.TimeoutExpired as error:
        raise AdminError("L'opération a pris trop de temps.") from error
    except OSError as error:
        raise AdminError(str(error)) from error
    if result.returncode == 0:
        return result.stdout
    message = (result.stderr or result.stdout).strip()
    lowered = message.lower()
    if command[0] == "pkexec":
        if "no authentication agent" in lowered or result.returncode == 127:
            raise NeedPassword()
        if result.returncode == 126:
            raise AdminCancelled()
    if command[0] == "sudo":
        if "incorrect password" in lowered or "password is required" in lowered or "sorry, try again" in lowered:
            raise WrongPassword()
        if "not in the sudoers" in lowered or "not allowed" in lowered:
            raise AdminError("Ton compte n'a pas le droit d'administrer ce système (sudo).")
    raise AdminError(message[:300] or f"Échec de {args[0]}.")
