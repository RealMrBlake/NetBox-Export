# NetBox-Export

NetBox-Export ist ein Plugin für NetBox 4.6.x. Es exportiert einen abgegrenzten
Mandanten- oder Standortbereich als portables ZIP-Archiv und importiert ihn in
eine zweite NetBox-Instanz.

Unterstützte Startpunkte:

- Mandantengruppe einschließlich Untergruppen und Mandanten
- einzelner Mandant
- Region einschließlich Unterregionen und Standorten
- einzelner Standort
- Lokation einschließlich Unterlokationen

Der Export folgt den Besitzbeziehungen zu DCIM-, IPAM-, Circuit-,
Virtualisierungs-, VPN-, Wireless-, Kontakt-, Tag- und Bilddaten. Benötigte
Stammdaten werden als Abhängigkeiten mitgenommen. Primärschlüssel der
Quellinstanz werden nie direkt als Zielschlüssel verwendet.

## Installation

Das Plugin muss auf beiden NetBox-Instanzen installiert sein.

```bash
cd /opt/netbox
source venv/bin/activate
pip install --upgrade --force-reinstall \
  "git+https://git.mrblake.cc/MrBlake/NetBox-Export.git@main"

# Prüfen, ob das Modul im NetBox-venv verfügbar ist
python -c "import netbox_export; print(netbox_export.__file__)"
```

In `configuration.py` ergänzen:

```python
PLUGINS = [
    "netbox_export",
]

PLUGINS_CONFIG = {
    "netbox_export": {
        "max_objects": 50000,
        "max_archive_size_mb": 250,
        # Auf beiden Instanzen identisch setzen, um Archive zu signieren.
        "archive_signing_key": "eine-lange-zufaellige-geheime-zeichenfolge",
    },
}
```

Anschließend:

```bash
cd /opt/netbox/netbox
python manage.py migrate netbox_export
python manage.py collectstatic --no-input
sudo systemctl restart netbox netbox-rq
```

Falls `ModuleNotFoundError: No module named 'netbox_export'` erscheint, wurde
das Paket nicht in `/opt/netbox/venv` installiert. In diesem Fall den obigen
Installationsblock erneut ausführen und darauf achten, dass
`/opt/netbox/venv/bin/python` verwendet wird:

```bash
/opt/netbox/venv/bin/python -m pip install --upgrade --force-reinstall \
  "git+https://git.mrblake.cc/MrBlake/NetBox-Export.git@main"
/opt/netbox/venv/bin/python -c \
  "import netbox_export; print(netbox_export.__file__)"
```

Bei einer Docker-Installation wird das Paket in das eigene NetBox-Image
aufgenommen; danach wird der Container mit dem aktivierten Plugin neu gebaut und
die Migration ausgeführt.

## Verwendung

Die Oberfläche liegt unter **Plugins > NetBox-Export > Export / Import** und ist
aus Sicherheitsgründen nur für Superuser sichtbar.

1. Auf Instanz A den Typ und das konkrete Objekt wählen und das ZIP exportieren.
2. Auf Instanz B das ZIP zunächst mit **Nur prüfen** verarbeiten.
3. Nach erfolgreichem Prüflauf **Nur prüfen** deaktivieren, den schreibenden
   Import bestätigen und das Archiv erneut hochladen.

Der Import läuft atomar. Bei einem Fehler werden alle Datenbankänderungen
zurückgerollt. Die Konfliktstrategie **Aktualisieren** nutzt zuerst die dauerhaft
gespeicherte Zuordnung aus Quellinstanz, Modell und Quell-ID; bei einem ersten
Import werden vorhandene Objekte über ihre eindeutigen Fachschlüssel erkannt.

## Verhalten und Grenzen

- Quelle und Ziel müssen NetBox 4.6.x und dieselben Plugins/Modelle verwenden.
- Benutzerkonten und Berechtigungen werden nicht exportiert. Referenzen auf
  Benutzer oder Gruppen müssen auf dem Ziel bereits eindeutig vorhanden sein.
- Der Import erstellt und aktualisiert Objekte. Zielobjekte, die im Archiv nicht
  vorkommen, werden bewusst nicht gelöscht.
- Fehlende Bilddateien werden im Archiv vermerkt, können aber nicht rekonstruiert
  werden.
- Große Exporte werden synchron verarbeitet. `max_objects` begrenzt Laufzeit und
  Speicherverbrauch.

## Tests

```bash
python -m pytest
```
