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
server.mjs              Tiny static dev server (correct wasm MIME + headers)
public/                 Our host app (HTML/CSS/JS shell around the engine)
vendor/onlyoffice/      Built AGPL engine (gitignored)
.build/                 Build workspace: source + tarball cache (gitignored)
```

## Getting started

```bash
npm run build-engine   # build the ONLYOFFICE PDF engine from source (Python 3)
npm start              # serve at http://localhost:3000
```

## Host integration

The host (`public/`) drives ONLYOFFICE's **standalone viewer** API, mirroring
upstream's own `sdkjs/pdf/test` harness but as our own minimal UI:

```js
const viewer = new AscViewer.CViewer("id_viewer", {
  sdkjsPath: "/vendor/onlyoffice/sdkjs",
  fontsPath: "/vendor/fonts/",
});
viewer.open(arrayBuffer);          // + setZoom / setZoomMode / rotatePage
viewer.createThumbnails("thumbnails");
```

The built engine bundle `pdf/src/engine/viewer.js` already includes the
high-level `CViewer` wrapper, so a single script provides the whole API. We open
local files entirely in the browser (FileReader → ArrayBuffer), so there is no
server upload and no Document Server.

## Required: the font registry (`AllFonts.js`)

The engine needs `vendor/onlyoffice/sdkjs/common/AllFonts.js` — a generated
registry of font metadata — to open **any** PDF. It is **not** in the sdkjs repo:
it is produced by ONLYOFFICE's `core` C++ tool `AllFontsGen` from the
[`core-fonts`](https://github.com/ONLYOFFICE/core-fonts) TTFs, alongside the TTF
files served from `fontsPath`.

The host preflights this file and shows an actionable message if it is missing.
Generating it from `core-fonts` (a Node port of `AllFontsGen`) is the next
milestone; until then, drop a prebuilt `AllFonts.js` + `fonts/` from any
ONLYOFFICE Docs/Desktop install into the paths above.

## Status

- ✅ AGPL licensing + attribution
- ✅ Reproducible from-source engine build (`npm run build-engine`)
- ✅ Zero-dependency dev server (correct wasm MIME, COOP/COEP)
- ✅ Standalone viewer host wired to `AscViewer.CViewer` (open, zoom, rotate,
     thumbnails, drag & drop) — needs in-browser verification
- ⏳ Font registry `AllFonts.js` + TTFs (blocks actual rendering)
- ⏳ Editing (annotations/forms/text) via the full `Asc.PDFEditorApi`
     (the `word/sdk-all.js` bundle is already built and vendored for this)
