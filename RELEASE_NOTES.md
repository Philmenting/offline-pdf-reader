# Offline PDF Editor 0.8.2

- Fehlende Windows-Standardschriften (WinAnsi/TrueType) werden durch passende
  eingebettete Liberation-Schriften ergänzt. Fett, Kursiv und Sonderzeichen
  bleiben dadurch bei der Bearbeitung korrekt zugeordnet.
- Bereits eingebettete Schriften, unbekannte Familien und andere Kodierungen
  werden nicht ersetzt.
- Bilder und Logos erzwingen keine erneute OCR des vorhandenen Seitentextes.
  Umlaute, Eurozeichen und m²/m³ bleiben in der durchsuchbaren Textebene erhalten.
- Automatische Regressionstests für Schrifteinbettung, Textseiten mit Bildern
  und Bearbeiten/Speichern ergänzt.

Die sichtbare Ausgabe bearbeiteter Textseiten bleibt technisch eine
300-dpi-Rasterdarstellung mit zusätzlicher Textebene. Nicht eingebettete
Microsoft-Schriften erhalten einen metrisch kompatiblen Ersatz. Für bereits
beschädigte Exporte bitte die ursprüngliche PDF erneut bearbeiten.

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
