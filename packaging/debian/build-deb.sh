#!/usr/bin/env bash
# Construit un paquet .deb : packaging/debian/build-deb.sh [dossier de sortie]
# La distribution doit fournir PySide6 (Debian 13 ou plus). Sinon, utilise install.sh.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
output="$(mkdir -p "${1:-$root/dist}" && cd "${1:-$root/dist}" && pwd)"
version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml")"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
package="$work/feuillet"

install -d "$package/DEBIAN" "$package/usr/lib/python3/dist-packages" "$package/usr/bin"
cp -r "$root/feuillet" "$package/usr/lib/python3/dist-packages/"
find "$package" -name __pycache__ -type d -prune -exec rm -rf {} +
printf '#!/bin/sh\nexec python3 -m feuillet "$@"\n' > "$package/usr/bin/feuillet"
chmod 755 "$package/usr/bin/feuillet"
install -Dm644 "$root/data/feuillet.desktop" "$package/usr/share/applications/feuillet.desktop"
install -Dm644 "$root/data/feuillet.svg" "$package/usr/share/icons/hicolor/scalable/apps/feuillet.svg"

cat > "$package/DEBIAN/control" <<CONTROL
Package: feuillet
Version: $version
Section: graphics
Priority: optional
Architecture: all
Maintainer: freezer71 <noreply@users.noreply.github.com>
Depends: python3 (>= 3.12), python3-pyside6.qtwidgets, python3-pyside6.qtsvg, python3-pyside6.qtdbus, python3-pil, python3-numpy, python3-cups, python3-pypdf, cups-client, cups-ipp-utils, poppler-utils, snmp, sane-utils, fonts-noto-core
Recommends: tesseract-ocr, tesseract-ocr-fra
Suggests: libreoffice
Description: Imprimer, scanner et assembler des documents
 Application de bureau pour imprimer via CUPS, scanner, fusionner et recadrer
 des documents, et imprimer des étiquettes Brother QL.
CONTROL

dpkg-deb --root-owner-group --build "$package" "$output/feuillet_${version}_all.deb"
echo "$output/feuillet_${version}_all.deb"
