# Depuis la racine du dépôt :
#   rpmbuild -bb packaging/fedora/feuillet.spec --define "_sourcedir $PWD" --define "_rpmdir $PWD/dist"
Name:           feuillet
Version:        0.1.0
Release:        1%{?dist}
Summary:        Imprimer, scanner et assembler des documents
License:        GPL-3.0-only
URL:            https://github.com/freezer71/feuillet
BuildArch:      noarch
BuildRequires:  python3-devel
Requires:       python3 python3-pyside6 python3-pillow python3-pillow-qt python3-numpy python3-cups python3-pypdf
Requires:       cups-client cups-ipptool poppler-utils net-snmp-utils sane-backends google-noto-sans-fonts
Recommends:     tesseract tesseract-langpack-fra

%description
Application de bureau pour imprimer via CUPS, scanner, fusionner et recadrer
des documents, et imprimer des étiquettes Brother QL.

%install
install -d %{buildroot}%{python3_sitelib} %{buildroot}%{_bindir}
cp -r %{_sourcedir}/feuillet %{buildroot}%{python3_sitelib}/
find %{buildroot}%{python3_sitelib} -name __pycache__ -type d -prune -exec rm -rf {} +
printf '#!/bin/sh\nexec python3 -m feuillet "$@"\n' > %{buildroot}%{_bindir}/feuillet
chmod 755 %{buildroot}%{_bindir}/feuillet
install -Dm644 %{_sourcedir}/data/feuillet.desktop %{buildroot}%{_datadir}/applications/feuillet.desktop
install -Dm644 %{_sourcedir}/data/feuillet.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/feuillet.svg
install -Dm644 %{_sourcedir}/LICENSE %{buildroot}%{_datadir}/licenses/feuillet/LICENSE

%files
%license %{_datadir}/licenses/feuillet/LICENSE
%{python3_sitelib}/feuillet/
%{_bindir}/feuillet
%{_datadir}/applications/feuillet.desktop
%{_datadir}/icons/hicolor/scalable/apps/feuillet.svg
