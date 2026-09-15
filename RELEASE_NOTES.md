# Offline PDF Editor 0.8.0

Aktuelle Windows-Ausgabe mit Installer und portabler ZIP. Die Versionsnummer
setzt die frühere Release-Reihe nach v0.7.4 fort.

## Verbesserungen

1. Gemeinsamer Exportweg für Speichern und Seitenextraktion, einschließlich bearbeiteter Texte und Formularwerte.
2. Bekannte Textzeilen werden in unterstützten Layouts ohne erneute OCR als durchsuchbarer Text exportiert.
3. Wiederhergestellte Dokumente behalten ihre Absturzsicherung bis zum Speichern oder ausdrücklichen Verwerfen.
4. Mehrere PDFs und Bilder lassen sich gemeinsam an einer gewählten Position einfügen und mit einem Schritt rückgängig machen.
5. Eine geschwärzte Kopie entfernt die gewählten Bildbereiche und übernimmt keine ursprünglichen Textobjekte, Anhänge oder Metadaten.
6. Kompakte PDF-Kopien bieten Qualitätsauswahl und Dateigrößenvergleich.
7. Zusätzliche Tests sichern Textbearbeitung, Formulare, Importe, Exporte und Wiederherstellung ab.

## Wichtige Grenzen

Schwärzung und Komprimierung erzeugen separate Bild-PDFs. Diese besitzen keine
Textsuche, interaktiven Formularfelder oder gültigen digitalen Signaturen.
Das geöffnete Original bleibt unverändert. Eine kleinere Datei ist nicht in jedem
Fall möglich.

Bearbeitete Textseiten werden beim normalen Export weiterhin sichtbar gerastert.
Die ergänzte Textebene ersetzt keinen vollständigen Vektorexport. OCR-Ergebnisse
sind vor der Weitergabe zu prüfen.

Die Anwendung läuft offline. Vor einem Einsatz im Verwaltungsnetz ist weiterhin
die Prüfung und Freigabe durch die zuständige IT erforderlich.
