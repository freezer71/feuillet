#!/usr/bin/env bash
# Installe le lanceur et l'entrée de menu pour l'utilisateur courant. Aucun droit root.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="$HOME/.local/bin"; apps="$HOME/.local/share/applications"; icons="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$bin" "$apps" "$icons"
cat > "$bin/imprimerie" <<SCRIPT
#!/usr/bin/env bash
PYTHONPATH="$root" exec /usr/bin/python3 -m imprimerie "\$@"
SCRIPT
chmod +x "$bin/imprimerie"
cp "$root/data/imprimerie.svg" "$icons/imprimerie.svg"
cat > "$apps/imprimerie.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Imprimerie
Comment=Imprimer et scanner
Exec=$bin/imprimerie %F
Icon=imprimerie
Terminal=false
Categories=Office;
MimeType=application/pdf;image/png;image/jpeg;image/webp;image/tiff;text/plain;
DESKTOP
echo "Installé : lance « imprimerie », ou cherche Imprimerie dans le menu."
