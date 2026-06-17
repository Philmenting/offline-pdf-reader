# Funktionslücken-Analyse: OnlyOffice PDF-Editor → Offline-PDF-Reader

**Stand:** 2026-06-17
**Branch:** `claude/zen-mendel-x181wj`
**Umfang (vom Auftraggeber festgelegt):** PDF-Editor-Funktionen + Konvertierung. **Ausgenommen: KI-Funktionen.**
**Vorgehen:** OnlyOffice wird *nicht* kopiert (AGPL-v3, inkompatibel mit dem MIT-Projekt und anderer Tech-Stack). Stattdessen dienen sein Look & sein Funktionsumfang als **Vorbild**, das in der bestehenden Python/PySide6-App (`app.py`) nachgebaut wird.

> **Umsetzungsfortschritt (P1 abgeschlossen):**
> - ✅ Text-Markup **Durchstreichen** + **Unterstreichen** (PyMuPDF `add_strikeout_annot`/`add_underline_annot`)
> - ✅ **Echter Druckdialog** (`Ctrl+P`, `QtPrintSupport`, Seitenbereich)
> - ✅ **Schriftformatierung** für Text-Werkzeuge (Familie Helvetica/Times/Courier + Fett/Kursiv)
> - ✅ **Kommentar-Panel** in der Sidebar (Liste aller Annotationen, Anspringen, Erledigt-Status)
>
> **Umsetzungsfortschritt (P2 abgeschlossen):**
> - ✅ **Ellipse/Kreis** als Form-Werkzeug
> - ✅ **Hyperlink** einfügen (Bereich + URL)
> - ✅ **Kopf-/Fußzeile** über Seitenbereich
> - ✅ **Tabelle** (Raster Zeilen×Spalten)
>
> Als Nächstes: P3 (Konvertierung) via LibreOffice-Headless. Textbearbeitung-Tiefe: echtes Reflow-WYSIWYG (großer Posten, später).

---

## 1. Methodik

- **Soll** = Funktionsumfang des OnlyOffice **PDF-Editors** (Tabs *Datei, Start, Schwärzen, Kommentar, Einfügen, Ansicht, Plugins*) + Konvertierungs-Funktionen. KI-Tab bewusst ausgeklammert.
- **Ist** = vorhandene Funktionen in `app.py` (Menüs, Toolbar-Ribbon-Gruppen, Annotations-Sidebar), erhoben aus dem Quellcode.
- **Status-Legende:**
  - ✅ **vorhanden** – funktional vergleichbar bereits umgesetzt
  - ◐ **teilweise** – Grundfunktion da, aber eingeschränkt ggü. OnlyOffice
  - ❌ **fehlt** – noch nicht vorhanden
  - 🚫 **außer Scope** – KI / Server-Kollaboration, bewusst nicht nachgebaut

---

## 2. OnlyOffice-PDF-Editor: Referenz-Ribbon

OnlyOffice gliedert den PDF-Editor in diese Tabs (ohne KI):

| Tab | Kernfunktionen |
|-----|----------------|
| **Datei** | Öffnen, Speichern, Speichern als / Herunterladen als (Konvertierung), Drucken, Dokumentinfo, Passwortschutz, Versionsverlauf*, Einstellungen |
| **Start** | Text bearbeiten (echte Inhalts­bearbeitung), Objekte auswählen/bearbeiten, Schriftformatierung (Font/Größe/Fett/Kursiv/Unterstrichen/Farbe/Hervorhebung), Absatz (Ausrichtung, Listen), Kopieren/Einfügen, Rückgängig/Wiederholen |
| **Schwärzen** | Bereiche zum Schwärzen markieren, Schwärzung anwenden (Inhalt unwiderruflich entfernen) |
| **Kommentar** | Kommentar hinzufügen, Text-Markup (Hervorheben/Unterstreichen/Durchstreichen), Kommentar-Thread, auflösen, navigieren |
| **Einfügen** | Seite, Tabelle, Bild, Formen, Textfeld, TextArt, Hyperlink, Kommentar, Kopf-/Fußzeile, Symbol, Gleichung |
| **Ansicht** | Zoom, An Seite/Breite anpassen, Seitenminiaturen, Navigationsleiste, Dunkelmodus, mehrseitige Ansicht, Ansicht drehen |
| **Plugins** | OCR, Übersetzung u. a. (in unserem Scope v. a. OCR) |
| ~~KI~~ | 🚫 ausgeschlossen |

\* Versionsverlauf = Server-/Kollaborationsfeature → außer Scope.

**Formulare:** Im OnlyOffice-PDF-Editor werden Formulare *ausgefüllt*; das *Erstellen* füllbarer Felder geschieht im Dokumenteditor und wird als PDF gespeichert.

**Konvertierung (von/zu PDF):** PDF→DOCX, PDF→TXT, PDF→Bilder; DOCX/XLSX/PPTX→PDF; Bilder→PDF.

---

## 3. Soll/Ist-Vergleich nach Tab

### 3.1 Datei

| OnlyOffice-Funktion | Status | Ist-Umsetzung in `app.py` |
|---|---|---|
| Öffnen / Schließen | ✅ | `open_pdf`, `close_pdf` |
| Speichern / Speichern als | ✅ | `save_in_place`, `save_as_suggested` |
| Dokumentinfo | ✅ | `show_document_info` |
| Metadaten bearbeiten | ✅ | `edit_pdf_metadata` (über OnlyOffice hinaus) |
| Passwortschutz / Entschlüsseln | ✅ | `export_encrypted_pdf_copy`, `export_decrypted_pdf_copy` |
| Optimieren / Komprimieren | ✅ | `export_optimized_pdf_copy` (über OnlyOffice hinaus) |
| **Drucken (echter Druckdialog)** | ✅ | `print_document` (Ctrl+P, `QPrintDialog`/`QPrinter`, Seitenbereich) |
| **Herunterladen als → Office-Format (Konvertierung)** | ❌ | siehe §4 |
| Versionsverlauf | 🚫 | Kollaboration/Server |

### 3.2 Start (Text & Bearbeitung)

| OnlyOffice-Funktion | Status | Ist-Umsetzung |
|---|---|---|
| Rückgängig / Wiederholen | ✅ | `undo_last_change`, `redo_last_change` |
| Text ersetzen (Inhaltsbearbeitung, einfach) | ◐ | `text-replace`-Werkzeug + Inline-Edit von FreeText/Formularfeldern |
| **Echte WYSIWYG-Textbearbeitung** (Inhalt im Textfluss editieren) | ❌ | nicht vorhanden – größte Lücke |
| **Schriftformatierung** (Font, Größe, Fett/Kursiv/Unterstrichen, Farbe) | ✅ | Familie (Helvetica/Times/Courier) + Fett/Kursiv + Größe + Farbe für Text-Werkzeuge (`_resolve_fontname`) |
| **Absatz** (Ausrichtung, Listen, Zeilenabstand) | ❌ | nicht vorhanden |
| Kopieren / Einfügen von Objekten | ❌ | nicht vorhanden |
| Format übertragen | ❌ | nicht vorhanden |

### 3.3 Schwärzen (Redact)

| OnlyOffice-Funktion | Status | Ist-Umsetzung |
|---|---|---|
| Bereich zum Schwärzen markieren | ✅ | `redact`-Werkzeug |
| Schwärzung final anwenden | ✅ | `apply_pending_redactions` |

➡️ **Vollständig abgedeckt.**

### 3.4 Kommentar

| OnlyOffice-Funktion | Status | Ist-Umsetzung |
|---|---|---|
| Notiz/Kommentar hinzufügen | ✅ | `note`-Werkzeug, `add_text_annotation` |
| Kommentar bearbeiten / Antwort | ◐ | `edit_selected_annotation_comment`, `reply_to_selected_annotation` (kein echter Thread/Resolve-Workflow) |
| Hervorheben (Highlight) | ✅ | `add_highlight_annotation` |
| **Durchstreichen (Strikeout)** | ✅ | `add_strikeout_annotation` (Werkzeug + Menü) |
| **Unterstreichen (Underline)** | ✅ | `add_underline_annotation` (Werkzeug + Menü) |
| Kommentar-Panel mit Navigation/Resolve | ✅ | Sidebar-Karte „Kommentare": Liste, Anspringen, Erledigt-Toggle (`_refresh_comment_list`, `toggle_selected_comment_resolved`) |

### 3.5 Einfügen

| OnlyOffice-Funktion | Status | Ist-Umsetzung |
|---|---|---|
| Leere Seite | ✅ | `insert_blank_page_after_current` |
| Bild / Signatur / Stempel | ✅ | `image`-Werkzeug, `pick_annotation_image` |
| Form: Rechteck | ✅ | `add_rectangle_annotation` |
| Form: Linie / Pfeil | ✅ | `add_line_annotation`, `add_arrow_annotation` |
| Freihand zeichnen | ✅ | `freehand`-Werkzeug |
| Textfeld | ✅ | Text-Werkzeug (FreeText-Annotation) deckt das Textfeld ab |
| **Tabelle** | ✅ | `insert_table` (Raster Zeilen×Spalten auf aktueller Seite) |
| **Weitere Formen** (Ellipse, Pfeile, Sterne, Callouts …) | ◐ | Rechteck/Ellipse/Linie/Pfeil/Freihand (`add_ellipse_annotation`); Sterne/Callouts noch offen |
| **Hyperlink** | ✅ | `add_link_annotation` (Bereich ziehen → URL, `insert_link` + sichtbare Linie) |
| **TextArt / WordArt** | ❌ | nicht vorhanden |
| **Symbol / Gleichung** | ❌ | nicht vorhanden |
| Kopf-/Fußzeile | ✅ | `insert_header_footer` (Kopf/Fuß × links/mittig/rechts, Seitenbereich) |
| Seitennummern | ✅ | `insert_page_numbers` |

### 3.6 Ansicht

| OnlyOffice-Funktion | Status | Ist-Umsetzung |
|---|---|---|
| Zoom +/−/zurücksetzen | ✅ | Zoom-Buttons |
| An Seite/Breite anpassen | ◐ | „Fit"-Button vorhanden, getrennte Modi Seite/Breite zu prüfen |
| Seitenminiaturen | ✅ | `ThumbnailListWidget` (linkes Panel) |
| Ansicht drehen | ✅ | `rotate_left`/`rotate_right`/Reset |
| Dunkelmodus | ✅ | `_is_dark_mode` + Dark-Styles |
| **Mehrseitige / fortlaufende Ansicht** | ❌ | zu prüfen / vermutlich Einzelseite |
| **Lesezeichen-/Outline-Navigation (TOC)** | ❌ | nicht vorhanden |

### 3.7 Plugins / OCR

| OnlyOffice-Funktion | Status | Ist-Umsetzung |
|---|---|---|
| OCR (Text aus Scan extrahieren) | ✅✅ | umfangreich: `extract_text_*`, Sprache, Korrekturmodus, Retry, durchsuchbare PDF – **über OnlyOffice hinaus** |
| Suche über alle Seiten | ✅ | `open_search`, next/prev, Trefferliste |
| Übersetzung / weitere Plugins | 🚫/❌ | nicht im Scope (KI-nah) |

### 3.8 Zusätzliche Stärken der App (über OnlyOffice-PDF hinaus)

- Stapel-Umbenennung ganzer Ordner mit Dry-Run (`batch_rename_folder`)
- Aggregierter Ordner-Export (TXT/JSON/CSV) (`export_folder_aggregate`)
- Dateinamen-Vorschlag aus Inhalt (`Datum_Typ_Absender_Nummer`)
- Leere Seiten entfernen, in Blöcke teilen, Graustufen-Export, Bilder extrahieren
- Visuelles Zuschneiden, Seiten duplizieren/neu anordnen

---

## 4. Konvertierungs-Matrix (Scope-relevant)

| Konvertierung | Status | Ist / Bedarf |
|---|---|---|
| Bilder → PDF | ✅ | `images_to_pdf` |
| PDF → Bilder | ✅ | `export_pages_as_images` |
| PDF → TXT/JSON/CSV | ✅ | `export_current_file` (auf OCR-/Textmodell) |
| PDF → durchsuchbares PDF (OCR-Layer) | ✅ | `export_searchable_pdf_copy` |
| **PDF → DOCX (editierbar)** | ❌ | schwer; Optionen: `pdf2docx`-Lib |
| **DOCX → PDF** | ❌ | benötigt LibreOffice-Headless (`soffice`) oder `docx2pdf` |
| **XLSX/PPTX → PDF** | ❌ | LibreOffice-Headless |
| **PDF → TXT (direkt, ohne OCR bei Text-PDF)** | ◐ | native Textextraktion vorhanden, eigener „Export als TXT direkt" prüfen |

**Hinweis Offline-Garantie:** DOCX/XLSX/PPTX↔PDF lässt sich offline am robustesten über ein **gebündeltes LibreOffice-Headless** (`soffice --headless --convert-to`) lösen. Reine Python-Libs (`pdf2docx`, `python-docx`) decken nur Teilfälle ab. Entscheidung nötig (siehe §6).

---

## 5. Priorisierte Lückenliste

**P1 – Kern-PDF-Editor (höchster Nutzen, OnlyOffice-Kerngefühl) — ✅ ABGESCHLOSSEN:**
1. ✅ Text-Markup vervollständigen: **Durchstreichen + Unterstreichen**.
2. ✅ **Echter Druckdialog** (`QtPrintSupport`/`QPrintDialog`).
3. ✅ **Schriftformatierung** für Text-/FreeText-Objekte (Font-Familie, Fett/Kursiv) im Eigenschaften-Panel.
4. ✅ **Kommentar-Panel** mit Liste, Navigation und Resolve-Status.

**P2 – Einfügen-Objekte — ✅ weitgehend abgeschlossen:**
5. ✅ **Ellipse/Kreis** ergänzt (`add_ellipse_annotation`); Sterne/Callouts optional später.
6. ✅ **Hyperlink** einfügen (`add_link_annotation`, PyMuPDF `insert_link`).
7. ✅ **Kopf-/Fußzeile** (`insert_header_footer`); Textfeld via Text-Werkzeug.
8. ✅ **Tabelle** einfügen (`insert_table`, einfaches Raster).

**P3 – Konvertierung (Scope „+ Konvertierung"):**
9. **DOCX/XLSX/PPTX → PDF** via LibreOffice-Headless (offline-fähig).
10. **PDF → DOCX** via `pdf2docx`.
11. „Herunterladen als"-Menü im Datei-Tab, das diese Pfade bündelt.

**P4 – Ansicht/Navigation:**
12. **Outline-/Lesezeichen-Navigation** (TOC-Panel aus PDF-Bookmarks).
13. **Fortlaufende mehrseitige Ansicht**, getrennte Modi „An Seite/An Breite".

**Außer Scope (nicht umsetzen):** KI-Funktionen, Echtzeit-Kollaboration/Versionsverlauf, eingebauter Chat/Telegram.

---

## 6. Offene Entscheidungen für die Umsetzungsphase

1. **OnlyOffice-Ribbon-Umbau:** bestehende Menü-/Toolbar-Struktur auf die OnlyOffice-Tabs (*Datei/Start/Schwärzen/Kommentar/Einfügen/Ansicht*) als echte getabbte Ribbon-Leiste umstellen — ja/nein und wann?
2. **Office-Konvertierung:** LibreOffice-Headless bündeln (robust, aber ~300 MB Abhängigkeit) **oder** reine Python-Libs (leichtgewichtig, aber lückenhaft)?
3. **Text-Editing-Tiefe:** „Text ersetzen/Box-weise" (heutiger Ansatz, realistisch) **oder** echtes Reflow-WYSIWYG (sehr aufwändig)?

---

## 7. Fazit

Die App deckt den OnlyOffice-PDF-Editor bereits **erstaunlich weit** ab — Anzeigen, Annotieren, Schwärzen, Seitenverwaltung, Formularfelder, Export und OCR sind vorhanden, teils über OnlyOffice hinaus. Die echten Lücken sind überschaubar und liegen in vier Bereichen: **(a) Text-Markup/-Formatierung & Druck, (b) reichere Einfügen-Objekte, (c) Office-Konvertierung, (d) Outline-Navigation.** Empfohlene Reihenfolge: P1 → P2 → P3 → P4, parallel dazu der optionale Ribbon-Umbau für das OnlyOffice-Erscheinungsbild.
