#!/usr/bin/env bash
# Installe Feuillet pour l'utilisateur courant, sur Arch, Debian, Ubuntu, Fedora ou openSUSE.
# Seules les dépendances du système demandent des droits root.
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
install_dir="$data_home/feuillet"
bin_dir="$HOME/.local/bin"
assume_yes=0
install_deps=1
dev_mode=0

usage() {
    cat <<'TEXT'
Usage : ./install.sh [options]

  --yes       Installe les dépendances sans demander de confirmation
  --no-deps   N'installe aucun paquet du système
  --dev       Lance le code de ce dossier au lieu d'en faire une copie
  --help      Affiche cette aide
TEXT
}

for argument in "$@"; do
    case "$argument" in
        --yes) assume_yes=1 ;;
        --no-deps) install_deps=0 ;;
        --dev) dev_mode=1 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Option inconnue : $argument" >&2; usage >&2; exit 2 ;;
    esac
done

say() { printf '\n==> %s\n' "$1"; }

run_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null; then
        sudo "$@"
    else
        echo "Droits root nécessaires, et sudo est absent. Relance avec --no-deps après avoir installé : $*" >&2
        exit 1
    fi
}

detect_family() {
    [ -r /etc/os-release ] || { echo inconnue; return; }
    # shellcheck disable=SC1091
    . /etc/os-release
    case " ${ID:-} ${ID_LIKE:-} " in
        *" arch "*) echo arch ;;
        *" debian "*|*" ubuntu "*) echo debian ;;
        *" fedora "*|*" rhel "*) echo fedora ;;
        *" suse "*|*" opensuse "*|*" opensuse-tumbleweed "*) echo suse ;;
        *) echo inconnue ;;
    esac
}

install_system_packages() {
    local family="$1" yes_flag=()
    case "$family" in
        arch)
            [ "$assume_yes" -eq 1 ] && yes_flag=(--noconfirm)
            run_root pacman -S --needed "${yes_flag[@]}" pyside6 qt6-svg python-pillow python-numpy python-pycups python-pypdf \
                cups poppler net-snmp sane tesseract tesseract-data-fra tesseract-data-eng noto-fonts
            ;;
        debian)
            [ "$assume_yes" -eq 1 ] && yes_flag=(-y)
            run_root apt-get update
            run_root apt-get install "${yes_flag[@]}" python3 python3-venv python3-pil python3-numpy python3-cups \
                cups-client cups-ipp-utils poppler-utils snmp sane-utils tesseract-ocr tesseract-ocr-fra fonts-noto-core \
                libegl1 libgl1 libxkbcommon0 libxkbcommon-x11-0 libdbus-1-3 libfontconfig1 libglib2.0-0 libxcb-cursor0
            ;;
        fedora)
            [ "$assume_yes" -eq 1 ] && yes_flag=(-y)
            run_root dnf install "${yes_flag[@]}" python3 python3-pillow python3-pillow-qt python3-numpy python3-cups python3-pypdf \
                python3-pyside6 cups-client cups-ipptool poppler-utils net-snmp-utils sane-backends tesseract \
                tesseract-langpack-fra google-noto-sans-fonts
            ;;
        suse)
            [ "$assume_yes" -eq 1 ] && yes_flag=(--non-interactive)
            run_root zypper "${yes_flag[@]}" install python3 python3-pip python3-Pillow python3-numpy python3-pycups \
                cups-client poppler-tools net-snmp sane-backends tesseract-ocr tesseract-ocr-traineddata-fra \
                noto-sans-fonts libglvnd libgthread-2_0-0 libxkbcommon0 libxkbcommon-x11-0 libdbus-1-3 fontconfig libxcb-cursor0
            ;;
        *)
            cat >&2 <<'TEXT'
Distribution non reconnue. Installe à la main : Python 3.12 ou plus, pycups, CUPS (lp, ipptool),
Poppler (pdftocairo, pdfinfo, pdftotext), snmpget, SANE (scanimage) et, en option, Tesseract.
Relance ensuite avec --no-deps.
TEXT
            exit 1
            ;;
    esac
}

# Affiche le nom pip de chaque module Python manquant.
missing_python_modules() {
    "$1" - <<'PYTHON'
import importlib
modules = {"PySide6.QtWidgets": "PySide6-Essentials", "PySide6.QtSvg": "PySide6-Essentials",
           "PySide6.QtDBus": "PySide6-Essentials", "PIL.ImageQt": "Pillow", "numpy": "numpy", "cups": "pycups", "pypdf": "pypdf"}
missing = []
for module, package in modules.items():
    try:
        importlib.import_module(module)
    except ImportError:
        if package not in missing:
            missing.append(package)
print(" ".join(missing))
PYTHON
}

family="$(detect_family)"
say "Distribution détectée : $family"
if [ "$install_deps" -eq 1 ]; then
    say "Installation des dépendances du système"
    install_system_packages "$family"
fi

command -v python3 >/dev/null || { echo "Python 3 est introuvable." >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || { echo "Feuillet demande Python 3.12 ou plus." >&2; exit 1; }

mkdir -p "$install_dir" "$bin_dir"
python="$(command -v python3)"
missing="$(missing_python_modules "$python")"
if [ -n "$missing" ]; then
    # La distribution ne fournit pas tout : le reste va dans un environnement dédié, à part du système.
    say "Modules absents du système, installés à part : $missing"
    python3 -m venv --system-site-packages "$install_dir/venv"
    python="$install_dir/venv/bin/python3"
    # shellcheck disable=SC2086
    # --ignore-installed : Qt et ses liaisons doivent venir ensemble, sans mélange avec le système.
    "$python" -m pip install --quiet --disable-pip-version-check --ignore-installed $missing
else
    rm -rf "$install_dir/venv"
fi

if [ "$dev_mode" -eq 1 ]; then
    code_dir="$source_dir"
else
    code_dir="$install_dir/app"
    rm -rf "$code_dir"
    mkdir -p "$code_dir"
    cp -r "$source_dir/feuillet" "$code_dir/"
    find "$code_dir" -name __pycache__ -type d -prune -exec rm -rf {} +
fi

say "Vérification"
QT_QPA_PLATFORM=offscreen PYTHONPATH="$code_dir" "$python" -c 'import feuillet.ui' || {
    echo "Feuillet ne se charge pas : une bibliothèque du système manque sans doute (voir ci-dessus)." >&2
    exit 1
}

cat > "$bin_dir/feuillet" <<SCRIPT
#!/usr/bin/env bash
PYTHONPATH="$code_dir" exec "$python" -m feuillet "\$@"
SCRIPT
chmod +x "$bin_dir/feuillet"

apps_dir="$data_home/applications"
icons_dir="$data_home/icons/hicolor/scalable/apps"
mkdir -p "$apps_dir" "$icons_dir"
cp "$source_dir/data/feuillet.svg" "$icons_dir/feuillet.svg"
sed "s|^Exec=.*|Exec=$bin_dir/feuillet %F|" "$source_dir/data/feuillet.desktop" > "$apps_dir/feuillet.desktop"

say "Feuillet est installé"
echo "Lance-le avec la commande « feuillet », ou depuis le menu des applications."
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "Attention : $bin_dir n'est pas dans ton PATH." ;;
esac
command -v tesseract >/dev/null || echo "Sans Tesseract, le texte des scans ne pourra pas être lu."
if command -v scanimage >/dev/null && ! scanimage -L 2>/dev/null | grep -qi epsonscan2; then
    echo "Pour scanner avec l'Epson, installe aussi le pilote epsonscan2 proposé par Epson."
fi
