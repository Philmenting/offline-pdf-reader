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

## Project layout

```
LICENSE                 GNU AGPL-3.0 (full text)
NOTICE                  Attribution to ONLYOFFICE / Ascensio System SIA
package.json
scripts/
  fetch-onlyoffice-pdf.mjs   Downloads pinned prebuilt engine -> vendor/
server.mjs              Tiny static dev server (correct wasm MIME + headers)
public/                 Our host app (HTML/CSS/JS shell around the engine)
vendor/onlyoffice/      Fetched AGPL engine assets (gitignored)
```

## Getting started

```bash
npm run fetch-engine   # download the pinned ONLYOFFICE PDF engine
npm start              # serve at http://localhost:3000
```

## Status

Foundation in place: AGPL licensing, engine vendoring, dev server. The host
application around the engine is under active development.
