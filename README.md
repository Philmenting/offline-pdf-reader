# offline-pdf-reader

An **offline, in-browser PDF editor** built on the open-source
[ONLYOFFICE sdkjs](https://github.com/ONLYOFFICE/sdkjs) PDF engine.

> License: **AGPL-3.0-only**. This project links and distributes ONLYOFFICE
> AGPL code, so the whole project is AGPL-3.0. See `LICENSE` and `NOTICE`.

## Goal

Use **only the PDF-editing component** of ONLYOFFICE (the `sdkjs/pdf` module)
to provide PDF viewing and editing in the browser — **without** running the
full ONLYOFFICE Document Server.

## How ONLYOFFICE's PDF stack is structured (research notes)

The `sdkjs/pdf` module has two layers:

1. **Prebuilt engine** (`pdf/src/engine/`) — committed binaries in the upstream
   repo, no C++ build required:
   - `drawingfile.wasm` (~10 MB) — the PDF parse/render core (compiled from the
     ONLYOFFICE `core` repo).
   - `drawingfile.js` — WASM loader glue.
   - `cmap.bin` (~9 MB) — CMap tables for CJK/font handling.
   - `viewer.js` — closure-compiled viewer bundle (rendering, scrolling,
     thumbnails, text selection, search).
   These are fetched by `npm run fetch-engine` into `vendor/onlyoffice/`
   (pinned to a specific upstream commit; not committed here — see `.gitignore`).

2. **Editor logic + UI** — the annotation/forms/text-editing logic lives in
   `pdf/src/*.js` (`document.js`, `GraphicObjects.js`, `annotations/`,
   `forms/`, `drawings/`) and is bundled into `sdk-all.js` by the sdkjs build.
   The full toolbar/panels UI lives in
   [`web-apps/apps/pdfeditor`](https://github.com/ONLYOFFICE/web-apps).

### Key coupling constraint

The viewer (`AscViewer.CViewer` === `CHtmlPage(id, api)`) is **driven by a full
editor API object** (`this.Api`, i.e. `Asc.PDFEditorApi`). It is not a
standalone "load a PDF into a canvas" widget — it expects the editor API to be
present. This means a genuine editor requires the sdkjs editor bundle, not just
the engine binaries.

## Building the engine from source

The PDF editor engine is built from the ONLYOFFICE `sdkjs` **`word`** product —
its config pulls in all 57 `pdf/src/*` modules (viewer, document, annotations,
forms, drawings) plus the shared engine. Upstream's build is **concatenation
only** (no Java/closure compiler), so it runs anywhere Python 3 is available.

`npm run build-engine` does this end to end:

1. downloads the `sdkjs` source tarball at a pinned commit (cached in `.build/`),
2. runs `python3 build/build.py --product word`,
3. vendors `deploy/sdkjs` into `vendor/onlyoffice/sdkjs/`.

Output (`sdk-all-min.js` ~3.4 MB, `drawingfile.wasm` ~10 MB, common assets) is
AGPL-3.0 and gitignored — produced on demand.

## Project layout

```
LICENSE                 GNU AGPL-3.0 (full text)
NOTICE                  Attribution to ONLYOFFICE / Ascensio System SIA
package.json
scripts/
  build-onlyoffice-pdf.mjs   Builds + vendors the sdkjs PDF engine
  generate-allfonts.mjs      Downloads core-fonts + generates AllFonts.js
server.mjs              Tiny static dev server (correct wasm MIME + headers)
public/                 Our host app (HTML/CSS/JS shell around the engine)
vendor/onlyoffice/      Built AGPL engine (gitignored)
.build/                 Build workspace: source + tarball cache (gitignored)
```

## Getting started

```bash
npm run build-engine        # build the ONLYOFFICE PDF engine from source (Python 3)
npm run generate-fonts      # download core-fonts + generate AllFonts.js registry
npm run fetch-ocr           # vendor the offline OCR stack (optional)
npm start                   # serve at http://localhost:3000
npm test                    # E2E typing smoke test (needs Chromium, see below)
npm run dist:win            # bundle the Windows desktop app (ZIP)
npm run dist:win:installer  # build the NSIS setup exe from the bundle (needs `nsis`)
```

The smoke test guards the typed-text pipeline (font selection table, odttf
obfuscation, per-character glyph fallback) and runs in CI before every
release. Locally it uses `playwright` if installed, or `playwright-core`
with `CHROMIUM_PATH=<path-to-chromium>`.

## Features & shortcuts

Text editing (page text + text boxes) with a formatting bar (font family,
size, bold/italic, color), highlight/underline/strikeout markers, shapes
(rectangle/ellipse/line/arrow), a freehand **pen** (ink annotations), a
reusable **signature** stamp (draw once, stored locally, Shift+click to
redraw), comments, images, page add/remove/rotate, undo/redo. The top toolbar
is a single icon row; page management and document tools live in the
**Seiten** and **Extras** dropdown menus. The status bar shows the current
page (with jump-to-page input) and a zoom selector.

A **hand tool** pans the page while dragging; the select tool does
drag-to-select text (Strg+C copies). The marker color is user-selectable via
the swatch next to the marker buttons. User-drawn shapes are baked into the
PDF bytes on save (the engine's save stream has no drawing serialization).
Pages can be reordered by **dragging thumbnails** in the sidebar, or via
"Seite verschieben" in the Seiten menu. Recently opened files are listed on
the start screen (desktop app), and unsaved sessions are snapshotted every
~90 s so a crash offers recovery on the next start.

**Seiten** menu (Stirling-PDF/PDFSam-style, fully offline):
- **PDF anhängen** — merge another PDF's pages onto the end of the current
  document.
- **Seiten extrahieren (Teilen)** — extract a page range (e.g. `1-3,5`) into a
  standalone new PDF file, without modifying the open document.
- **Seitenbereich entfernen** — delete a page range (e.g. `2-4,7`) at once;
  undoable.
- **Alle Seiten drehen** — rotate every page in the document 90° in one click
  (for sideways scans), plus single-page rotate left/right.

**Extras** menu (Stirling-PDF/OmniTools-style, fully offline):
- **Wasserzeichen** — insert a text watermark centered on every page.
- **Seitenzahlen** — insert automatic "N / total" page numbers, bottom-center
  on every page.
- **Bilder extrahieren** — pull every embedded picture out of the PDF as its
  own file, de-duplicating a logo repeated across pages.
- **Seiten als Bilder exportieren** — export a page range as standalone PNG
  images at a chosen DPI, without navigating to each page first.
- **Durchsuchbar machen (OCR)** — recognize text in scanned pages fully
  offline (tesseract.js, German + English models vendored via
  `npm run fetch-ocr`) and embed it as an invisible text layer, so search,
  select and copy work in scans; or export the recognized text as .txt.

| Shortcut | Action |
| --- | --- |
| `Strg+O` | PDF öffnen |
| `Strg+S` | Speichern (Desktop-App: nativer Dialog, Web: Download) |
| `Strg+P` | Drucken (Desktop: Standard-PDF-App, Web: neuer Tab) |
| `Strg+F` | Suchen (Enter/F3 nächster, Umschalt+Enter voriger Treffer) |
| `Strg+Mausrad` | Zoomen |
| `Strg+C` | Auswahl kopieren |
| `Strg+Z` / `Strg+Y` | Rückgängig / Wiederholen |

The desktop app shows unsaved changes as `•` in the window title and warns
before closing.

## Host integration

The host (`public/`) drives ONLYOFFICE's **full PDF editor API**
(`Asc.PDFEditorApi`) — the same API the upstream `pdfeditor` web-app uses — not
just the read-only viewer. The editor lives in the `word` product bundle
(`word/sdk-all-min.js`) that `npm run build-engine` produces.

```js
// 1. load the font registry + the editor bundle
//    /vendor/onlyoffice/sdkjs/common/AllFonts.js
//    /vendor/onlyoffice/sdkjs/word/sdk-all-min.js
// 2. pin the engine asset base URL (drawingfile.wasm / cmap.bin)
window.AscViewer.baseEngineUrl = "/vendor/onlyoffice/sdkjs/pdf/src/engine/";

// 3. construct the editor — it builds its page DOM into #editor_sdk and
//    creates WordControl (CEditorPage) via _onEndLoadSdk()
const editor = new Asc.PDFEditorApi({ "id-view": "editor_sdk" });
editor.baseFontsPath = "/vendor/fonts/";

// 4. open an in-memory PDF — no server, no Document Server, no upload
editor.openDocument({ data: uint8Array });

// editing → real API methods
editor.AddFreeTextAnnot(...);                   // text box
editor.SetMarkerFormat(type, true, op, r,g,b);  // highlight / underline / strikeout
editor.StartAddShape("rect", false);            // shapes
editor.asc_AddPage / asc_RemovePage / asc_RotatePage;
editor.Undo() / editor.Redo();

// save the edited PDF back out (in-WASM serializer) and download it
const bytes = editor.getDocumentRenderer().Save();
```

If the editor bundle is missing the host falls back to the read-only
`AscViewer.CViewer` so opening a PDF still works.

### Editing UI (`public/`)

The toolbar wires the editor API to: text editing, text boxes, highlight /
underline / strikeout markers, shapes, comments, image insert, page
add / delete / rotate, undo / redo, and **Save** (downloads the edited PDF). All
local, all offline.

## Font registry (`AllFonts.js`)

The engine needs `vendor/onlyoffice/sdkjs/common/AllFonts.js` — a generated
registry of font metadata — to open **any** PDF. `npm run generate-fonts` builds
it automatically:

1. Downloads TTF/OTF files from [`ONLYOFFICE/core-fonts`](https://github.com/ONLYOFFICE/core-fonts)
   (cached in `.build/`).
2. Parses TTF name/OS2/head tables to extract family names and style variants.
3. Groups fonts into families (Regular, Italic, Bold, Bold Italic).
4. Generates `vendor/onlyoffice/sdkjs/common/AllFonts.js` (the registry) and
   copies the font files to `vendor/fonts/`.

Output: ~120 font families, ~189 font files. All gitignored.

## Status

- ✅ AGPL licensing + attribution
- ✅ Reproducible from-source engine build (`npm run build-engine`)
- ✅ Zero-dependency dev server (correct wasm MIME, COOP/COEP)
- ✅ Font registry `AllFonts.js` + TTFs generated from core-fonts (`npm run generate-fonts`)
- ✅ Full **PDF editor** host wired to `Asc.PDFEditorApi` (text boxes, highlight/
     underline/strikeout, shapes, comments, images, page add/delete/rotate,
     undo/redo, Save → download edited PDF) — built on `word/sdk-all-min.js`
- ✅ Read-only `AscViewer.CViewer` fallback if the editor bundle is unavailable
- ⏳ In-browser / Windows-build verification of the editor bootstrap (open a PDF,
     make edits, Save) — run `npm run build-engine && npm run generate-fonts`
     then `npm run dev` or build the desktop ZIP with `npm run dist:win`
