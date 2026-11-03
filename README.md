# Imprimerie

Application Qt pour imprimer et scanner, sans passer par les logiciels Epson.

- **Epson XP-6000** : impression via CUPS, scan via le pilote `epsonscan2` (sans son interface), niveaux d'encre.
- **Brother QL-1060N** : raster généré par l'application et envoyé en direct sur le port 9100.

## Utilisation

    ./install.sh
    imprimerie fichier.pdf

Pour un bon de retour : ouvre le PDF, choisis la Brother, trace un rectangle autour de
l'étiquette, vérifie avec « Voir le résultat sur l'étiquette », puis imprime.

Si une imprimante change d'adresse : page **Réglages**, « Rechercher les imprimantes ».

## Éditeur (Scanner et PDF)

Chaque scan ajoute une page en bas de la liste. Ajoute aussi des PDF ou des images pour les
fusionner. Glisse les pages pour les réordonner, recadre-les à la souris, choisis un format
(A4, A5…), retouche-les, puis exporte tout ou seulement la sélection.

- **Zoom** : molette, Ctrl++ / Ctrl+-, Ctrl+0 pour revenir à la page entière.
- **Déplacement** : outil Déplacer, ou bouton du milieu de la souris.
- **Texte** : l'outil Texte lit la page (texte du PDF, sinon OCR) ; glisse puis Ctrl+C.
- **Scan** : F5. Les pages et les scans sont conservés d'un lancement à l'autre, dans
  `~/.local/share/imprimerie/session`, jusqu'à « Nouveau document ».

Attention : recadrer une page de PDF masque le reste sans l'effacer du fichier. Pour supprimer
réellement une information sensible, coche l'OCR à l'export : les pages deviennent des images.

## Dépendances (Arch)

`pyside6 python-pillow python-numpy python-pycups python-pypdf cups poppler net-snmp sane epsonscan2`
Facultatif : `tesseract tesseract-data-fra` (OCR), `libreoffice-fresh` (formats Office).

## Tests

    /usr/bin/python3 -m unittest discover -s tests -t .
