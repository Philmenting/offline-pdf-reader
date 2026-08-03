/**
 * Host for the offline PDF *editor*, built on the full ONLYOFFICE PDF editor
 * API (`Asc.PDFEditorApi`) — not just the read-only standalone viewer.
 *
 * The `word` product bundle (`word/sdk-all-min.js`) that `npm run build-engine`
 * produces contains the complete PDF editor: viewer, annotations (text box,
 * highlight/underline/strikeout, ink, shapes, stamps), forms, comments, page
 * operations, history (undo/redo) and the in-WASM PDF serializer used to write
 * edits back out. We drive it directly, mirroring ONLYOFFICE's own editor
 * bootstrap (the editor builds its page DOM into `#editor_sdk`).
 *
 * Bootstrap sequence (matches sdkjs internals):
 *   1. load common/AllFonts.js (font registry) + word/sdk-all-min.js (editor)
 *   2. pin the engine asset base URL (drawingfile.wasm / cmap.bin live under
 *      /vendor/onlyoffice/sdkjs/pdf/src/engine/)
 *   3. new Asc.PDFEditorApi({ "id-view": "editor_sdk" }) → its loadSdk callback
 *      runs _onEndLoadSdk(), which creates WordControl (CEditorPage) and writes
 *      the editor page DOM into #editor_sdk
 *   4. editor.openDocument({ data }) opens an in-memory PDF (no server, no
 *      upload); the renderer loads the WASM engine and renders.
 *
 * If the editor bundle is unavailable we fall back to the read-only
 * AscViewer.CViewer so opening a PDF still works.
 */

const SDKJS_PATH    = "/vendor/onlyoffice/sdkjs";
const ENGINE_DIR    = `${SDKJS_PATH}/pdf/src/engine/`;   // trailing slash required
const FONTS_PATH    = "/vendor/fonts/";
const ALLFONTS      = `${SDKJS_PATH}/common/AllFonts.js`;
const APPLY_CHANGES = `${SDKJS_PATH}/common/applyDocumentChanges.js`;
// The editor build is split into two concatenated bundles: sdk-all-min.js
// (core API shell) and sdk-all.js (the bulk of the editor: History, document
// model, annotations, drawings). Both are required, min first.
const EDITOR_MIN    = `${SDKJS_PATH}/word/sdk-all-min.js`;
const EDITOR_COMMON = `${SDKJS_PATH}/word/sdk-all.js`;
const VIEWER_BUNDLE = `${SDKJS_PATH}/pdf/src/engine/viewer.js`;

// Third-party libs the sdkjs editor core expects as globals *before* the
// bundle runs (otherwise sdk-all-min.js throws "XRegExp is not defined" /
// "jQuery is not defined" and never defines Asc.PDFEditorApi). polyfill is
// optional on modern engines; jQuery + XRegExp are required.
const REQUIRED_LIBS = [
  `${SDKJS_PATH}/vendor/jquery.min.js`,
  `${SDKJS_PATH}/vendor/xregexp-all-min.js`,
];
const OPTIONAL_LIBS = [
  `${SDKJS_PATH}/vendor/polyfill.js`,
];

import { el, setStatus, loadScript, downloadBytes, downloadDataUrl } from "./modules/dom.js";
import { showPromptDialog, wirePromptDialog } from "./modules/prompt-dialog.js";
import { wireSearchBar, openSearchBar, searchStep } from "./modules/search.js";
import { wireStatusBar, updatePageCount, updateCurrentPage, updateZoomDisplay, setStatusControlsVisible } from "./modules/statusbar.js";
import { initRecovery, offerRecovery, clearRecoverySnapshot } from "./modules/recovery.js";
import { renderRecentFiles } from "./modules/recent-files.js";
import { storeGet, storeSet, openAppDb } from "./modules/storage.js";
import { wireSignatureDialog, insertSignature, openSignaturePad } from "./modules/signature.js";
import { initOcr, makeSearchablePdf, exportRecognizedText, ocrStackAvailable, recognizeCanvasWords, embedWordsOnPdfPage } from "./modules/ocr.js";

const ZOOM_STEPS = [50, 75, 90, 100, 110, 125, 150, 175, 200, 250, 300, 400];
const ZOOM_MODE  = { Custom: 0, Width: 1, Page: 2 };

let editor = null;       // Asc.PDFEditorApi instance (editor mode)
let viewer = null;       // AscViewer.CViewer instance (fallback mode)
let thumbnails = null;
let mode = "loading";    // "editor" | "viewer" | "loading"
let docOpen = false;
let lastName = "document.pdf";
let activeTool = "select";
let editorErrorMsg = null; // why we fell back to read-only mode (if we did)

// A text edit is baked into a fresh PDF so it can be selected and marked again.
// Keep a small hand-off state across that one required editor restart.
const TEXT_COMMIT_TRANSITION_KEY = "offline-pdf-editor:text-commit-transition";
let textCommitReloadRequested = false;
let textCommitReloadPending = false;

// Font-name resolution is data-driven by g_fonts_selection_bin, which our
// generated AllFonts.js now ships with real per-face records (panose, unicode/
// codepage ranges, metrics) for every bundled font — see
// scripts/generate-allfonts.mjs. With that table present the engine's own
// picker (g_fontApplication.GetFontFileWeb → penalty scoring, incl. its
// built-in Arial→Liberation Sans style aliases) resolves any requested name to
// a real bundled font. Earlier revisions monkey-patched pickFont /
// GetFontFileWeb / GetFontBySymbol to work around the empty table, which only
// treated symptoms: names the patches didn't know still fell through to the
// engine's ASCW3 dummy mini-font and rendered as .notdef boxes.
if (typeof window["g_fonts_selection_bin"] === "undefined") {
  window["g_fonts_selection_bin"] = "";
}

// Pin the engine asset base URL so the renderer fetches drawingfile.js/.wasm and
// cmap.bin from our vendored location. PDFEditorApi.openDocument() hardcodes a
// relative path here, so we make the property non-writable to keep our value.
function lockEngineBaseUrl() {
  const av = (window.AscViewer = window.AscViewer || {});
  for (const key of ["baseUrl", "baseEngineUrl"]) {
    try {
      Object.defineProperty(av, key, {
        configurable: true,
        enumerable: true,
        get() { return ENGINE_DIR; },
        set() { /* keep our pinned value */ },
      });
    } catch {
      av[key] = ENGINE_DIR;
    }
  }
}

// ── Editor bootstrap ──────────────────────────────────────────────────────
let sdkLoadError = null; // captured if _onEndLoadSdk throws inside the ctor

async function initEditor() {
  setStatus(textCommitReloadPending ? "Textänderungen werden vorbereitet …" : "PDF-Editor wird geladen …");

  // Third-party libs first (jQuery, XRegExp). polyfill is best-effort.
  console.log("[bootstrap] loading third-party libs (jquery, xregexp) …");
  for (const lib of OPTIONAL_LIBS) {
    try { await loadScript(lib); } catch (e) { console.warn(`[bootstrap] optional lib skipped: ${lib}`, e); }
  }
  for (const lib of REQUIRED_LIBS) await loadScript(lib);
  if (typeof window.jQuery !== "function") throw new Error("jQuery fehlt (vendor/jquery.min.js).");
  if (typeof window.XRegExp === "undefined") throw new Error("XRegExp fehlt (vendor/xregexp-all-min.js).");

  // Font registry + change-applier, then BOTH editor bundles (min then common).
  console.log("[bootstrap] loading AllFonts.js + editor bundles (min + common) …");
  await loadScript(ALLFONTS);
  try { await loadScript(APPLY_CHANGES); } catch (e) { console.warn("[bootstrap] applyDocumentChanges.js skipped", e); }
  await loadScript(EDITOR_MIN);
  await loadScript(EDITOR_COMMON);

  if (!(window.Asc && typeof window.Asc.PDFEditorApi === "function")) {
    throw new Error("Editor-Bundle geladen, aber Asc.PDFEditorApi fehlt.");
  }
  if (!(window.AscCommon && window.AscCommon.History)) {
    throw new Error("Editor-Bundle unvollständig: AscCommon.History fehlt (sdk-all.js nicht geladen?).");
  }
  console.log("[bootstrap] editor bundles loaded; Asc.PDFEditorApi + History present");

  // CGlobalFontLoader (common/GlobalLoaders.js) hardcodes fontFilesPath to
  // "../../../../fonts/" — a DIFFERENT path variable than Api.baseFontsPath
  // (which only reaches the WASM engine's own font fetches). Left unpatched,
  // every font-completion check 404s, CheckFontLoadStyles() never returns
  // false, and the loader's 50ms poll (check_loaded_timer_id) never stops:
  // isWorking() stays true forever, so the viewer's checkReady() never
  // dispatches "onFileOpened" and the document never reaches real
  // content-ready — which is also why text input never activates.
  if (window.AscCommon && window.AscCommon.g_font_loader) {
    window.AscCommon.g_font_loader.fontFilesPath = FONTS_PATH;
    console.log(`[bootstrap] patched g_font_loader.fontFilesPath -> ${FONTS_PATH}`);
  }

  // AscCommon.sendImgUrls ships a build meant for either a collaborative
  // Document Server (wordcopypaste.js) or the native desktop shell
  // (Local/common.js, calling window.AscDesktopEditor.LocalFileGetImageUrl).
  // We're neither: a bare Electron/browser page with no server and no
  // AscDesktopEditor global. Left as-is, the Local/common.js version would
  // throw (AscDesktopEditor is undefined) or — depending on load order — a
  // stub silently never calls its callback. Either way, CPDFDoc.EditPage()
  // (entered via "Text" mode) calls this for every inline data:-URI picture
  // on the page and awaits the callback before registering that image with
  // the loader that actually paints it; since the callback never fires, the
  // picture never gets registered and permanently renders as an empty
  // placeholder box with just its shape name (e.g. a logo showing only its
  // alt-text). Our images are always already-self-contained data: URIs, so
  // there's nothing to upload/resolve — answer synchronously with the same
  // URI as both fields, exactly the shape the callers expect back.
  if (window.AscCommon) {
    window.AscCommon.sendImgUrls = function (api, images, callback) {
      callback(images.map((src) => ({ url: src, path: src })));
    };
    console.log("[bootstrap] patched AscCommon.sendImgUrls -> synchronous offline passthrough");
  }

  // `AscCommon.loadSdk(name, onSuccess, onError)` is normally provided by the
  // web-apps script loader (it lazy-loads the SDK chunks). We ship the SDK as a
  // single pre-loaded bundle, so the SDK is already present. Force-install a
  // shim that runs the success callback immediately — and capture any error
  // thrown synchronously by _onEndLoadSdk() (which runs inside the api ctor) so
  // we can report the real reason instead of a generic timeout. Without this,
  // the ctor's _init() → loadSdk() never calls back and the editor page DOM /
  // WordControl never gets built.
  window.AscCommon = window.AscCommon || {};
  window.AscCommon.loadSdk = function (name, onSuccess, onError) {
    try { onSuccess && onSuccess(); }
    catch (e) {
      sdkLoadError = e;
      console.error("[bootstrap] _onEndLoadSdk threw:", e);
      if (onError) onError(e);
    }
  };

  lockEngineBaseUrl();

  console.log("[bootstrap] constructing Asc.PDFEditorApi …");
  try {
    editor = new window.Asc.PDFEditorApi({
      "id-view": "editor_sdk",
      "embedded": false,
    });
  } catch (e) {
    throw new Error(`PDFEditorApi-Konstruktor fehlgeschlagen: ${e && e.message ? e.message : e}`);
  }
  if (sdkLoadError) {
    throw new Error(`Editor-SDK-Initialisierung fehlgeschlagen: ${sdkLoadError.message || sdkLoadError}`);
  }
  window.__pdfEditor = editor; // console/debug access (main.js is a module)
  // Where the engine loads bundled TTFs from (sdkjs reads Api.baseFontsPath).
  editor.baseFontsPath = FONTS_PATH;

  // We open the document directly (no Document Server handshake), so set the
  // minimal identity that editing ops (history, comments, annotation authoring)
  // expect — normally established by asc_setDocInfo / the native open path.
  try {
    editor.documentId = editor.documentId || "offline";
    editor.documentUserId = editor.documentUserId || "offline-user";
    if (window.AscCommon && window.AscCommon.asc_CUser) {
      const user = new window.AscCommon.asc_CUser();
      user.setId("offline-user");
      user.setUserName("Offline");
      editor.User = user;
    }
  } catch (e) {
    console.warn("Konnte Benutzer/Dokument-Identität nicht setzen:", e);
  }

  registerEditorCallbacks();

  // Wait until loadSdk has run _onEndLoadSdk() (WordControl + page DOM built).
  await waitFor(
    () => editor.isLoadFullApi === true && !!editor.WordControl,
    15000,
    `Editor-SDK wurde nicht rechtzeitig initialisiert (isLoadFullApi=${editor.isLoadFullApi}, WordControl=${!!editor.WordControl}).`
  );
  console.log("[bootstrap] editor SDK ready (WordControl built)");

  if (typeof window.AscViewer.checkApplicationScale === "function") {
    window.AscViewer.checkApplicationScale();
  }

  mode = "editor";
  window.__pdfEditorReady = true; // stable readiness signal for integrations/tests
  installSubsetFontNameNormalization();
  installLongActionWatchdog();
  if (!textCommitReloadPending) setStatus("PDF-Editor ist bereit — bitte PDF öffnen.");

  // Desktop only: accept files from "Öffnen mit"/double-click (delivered by
  // the main process once we signal readiness).
  if (window.desktop && typeof window.desktop.onOpenFile === "function") {
    window.desktop.onOpenFile((payload) => {
      if (payload && payload.data && payload.data.buffer) {
        // slice the exact view: IPC buffers can be pooled/offset
        const d = payload.data;
        const buf = d.buffer.slice(d.byteOffset, d.byteOffset + d.byteLength);
        openArrayBuffer(buf, payload.name || "dokument.pdf");
      }
    });
    if (typeof window.desktop.rendererReady === "function") window.desktop.rendererReady();
  }

  // a document handed over by a pre-reload session (second file opened)
  const pending = await takePendingOpen();
  if (pending && pending.bytes) {
    pendingKeepDirty = !!pending.keepDirty;
    openArrayBuffer(pending.bytes.buffer, pending.name || "dokument.pdf");
  }

  // Crash recovery: if nothing opened by itself (pending doc, "Öffnen mit"
  // argv file), offer a leftover unsaved-session snapshot.
  setTimeout(() => { if (!docOpen) offerRecovery(); }, 1500);
}

// ── Long-action watchdog ──────────────────────────────────────────────────
// Diagnostics proved keystrokes are swallowed by onKeyDown's very first gate:
// isLongAction() stays true because IsLongActionCurrent is stuck at 1 — some
// sync_StartAction(BlockInteraction, …) during our offline open never gets its
// matching sync_EndAction (that pairing normally completes through Document-
// Server callbacks we bypass). Rather than chase each leaking action id, wrap
// Start/End to track outstanding actions (and name the culprit in the log),
// and force-end any BlockInteraction action that stays open although no real
// work (font/image loading) is running anymore. This heals the whole class of
// "stuck open action blocks all input forever" failures.
function installLongActionWatchdog() {
  const outstanding = new Map(); // "type:id" -> { type, id, count, since }
  const origStart = editor.sync_StartAction.bind(editor);
  const origEnd = editor.sync_EndAction.bind(editor);
  const BLOCK = window.Asc && window.Asc.c_oAscAsyncActionType
    ? window.Asc.c_oAscAsyncActionType.BlockInteraction : 1;

  editor.sync_StartAction = function (type, id, actionRestriction) {
    const key = `${type}:${id}`;
    const entry = outstanding.get(key) || { type, id, count: 0, since: 0 };
    entry.count++;
    entry.since = Date.now();
    outstanding.set(key, entry);
    return origStart(type, id, actionRestriction);
  };
  editor.sync_EndAction = function (type, id, actionRestriction) {
    const key = `${type}:${id}`;
    const entry = outstanding.get(key);
    if (entry && --entry.count <= 0) outstanding.delete(key);
    return origEnd(type, id, actionRestriction);
  };

  setInterval(() => {
    if (!editor || !editor.IsLongActionCurrent) return;
    let busy = false;
    try { busy = window.AscCommon.g_font_loader.isWorking(); } catch { /* ignore */ }
    try { busy = busy || (window.AscCommon.g_image_loader && window.AscCommon.g_image_loader.bIsLoadDocumentImages); } catch { /* ignore */ }
    if (busy) return; // genuine work still running — leave the counter alone

    const now = Date.now();
    for (const entry of [...outstanding.values()]) {
      if (entry.type === BLOCK && now - entry.since > 4000) {
        console.warn(`[action-debug] force-ending stuck action type=${entry.type} id=${entry.id} (open for ${((now - entry.since) / 1000) | 0}s, count=${entry.count})`);
        while (entry.count-- > 0) { try { origEnd(entry.type, entry.id); } catch { /* ignore */ } }
        outstanding.delete(`${entry.type}:${entry.id}`);
      }
    }
    // Counter still stuck with nothing tracked (leak predates the hook or an
    // internal path bypassed sync_EndAction): hard-reset as last resort.
    if (editor.IsLongActionCurrent > 0 && outstanding.size === 0) {
      console.warn(`[action-debug] IsLongActionCurrent=${editor.IsLongActionCurrent} with no tracked open actions — hard reset to 0`);
      editor.IsLongActionCurrent = 0;
    }
  }, 2000);
}

// Typing into text that uses an embedded/subset PDF font is handled by the
// engine itself (CPdfDrawingPrototype.EnterText): characters the subset still
// covers are inserted as GID-addressed items in the embedded font; characters
// the subset lacks fall back to a real font resolved through
// g_fontApplication.GetFontInfo(<subset name>). That resolution gets a raw
// subset PostScript name like "AAAAAA+LiberationSerif-Bold 4C33…B3"
// (tag + family + registry hash), which the penalty scorer cannot relate to
// any bundled family — it would fall through to the default font. Normalise
// such names to their underlying family ("LiberationSerif-Bold") before
// scoring; the scorer's prefix matching then picks the right family.
function installSubsetFontNameNormalization() {
  const app = window.AscFonts && window.AscFonts.g_fontApplication;
  if (!app || typeof app.GetFontFileWeb !== "function" || app.__subsetNamePatched) return;

  const orig = app.GetFontFileWeb;
  app.GetFontFileWeb = function (name, lStyle) {
    if (typeof name === "string" && /^[A-Z]{6}\+/.test(name) && undefined === this.FontPickerMap[name]) {
      const clean = name
        .replace(/^[A-Z]{6}\+/, "")
        .replace(/\s+[0-9A-Fa-f]{16,}$/, "");
      const font = orig.call(this, clean, lStyle);
      this.FontPickerMap[name] = font;
      console.log(`[fonts] Subset-Name normalisiert: "${name}" -> "${font.m_wsFontName}"`);
      return font;
    }
    return orig.call(this, name, lStyle);
  };
  app.__subsetNamePatched = true;
}

// ── Text formatting toolbar ───────────────────────────────────────────────
// Wires the font/size/bold/italic/color controls to the editor's own text-
// property API. State flows back through the engine's sync events
// (asc_onFontFamily / asc_onFontSize / asc_onBold / asc_onItalic), so the
// controls always reflect the text at the cursor/selection. The font list is
// a curated set of bundled families plus common aliases (Arial, Times New
// Roman, Courier New) that the selection table maps onto metric equivalents.
const fmtState = { bold: false, italic: false };

// Format controls (selects, color …18941 tokens truncated…
    const { PDFDocument, StandardFonts, rgb, degrees } = window.PDFLib;
    const pdf = await PDFDocument.load(bytes);
    const font = await pdf.embedFont(StandardFonts.Helvetica);
    const angleDeg = 45; // ascending bottom-left → top-right, like Word
    const rad = angleDeg * Math.PI / 180;
    for (const page of pdf.getPages()) {
      const { width, height } = page.getSize();
      const diag = Math.sqrt(width * width + height * height);
      // font size such that the text spans ~60% of the page diagonal
      const size = Math.max(20, Math.min(140, (diag * 0.6) / font.widthOfTextAtSize(text, 1)));
      const len = font.widthOfTextAtSize(text, size);
      const capH = font.heightAtSize(size) * 0.7;
      // drawText rotates counterclockwise around the baseline start — place
      // that origin so the rotated text's centre lands on the page centre
      const x = width / 2 - (len / 2) * Math.cos(rad) + (capH / 2) * Math.sin(rad);
      const y = height / 2 - (len / 2) * Math.sin(rad) - (capH / 2) * Math.cos(rad);
      page.drawText(text, {
        x, y, size, font,
        color: rgb(0.75, 0.75, 0.75), opacity: 0.4, rotate: degrees(angleDeg),
      });
    }
    const outBytes = await pdf.save();
    // the new bytes contain the full current state — skip the reopen confirm
    markDirty(false);
    setStatus(`Wasserzeichen „${text}" dauerhaft auf ${pdf.getPageCount()} Seite(n) eingebettet.`);
    openArrayBuffer(outBytes.buffer, lastName);
  } catch (e) {
    console.error("Wasserzeichen fehlgeschlagen:", e);
    setStatus(`Wasserzeichen fehlgeschlagen: ${e.message}`);
  }
}

// Automatic page numbers ("N / total"), bottom-center on every page.
function addPageNumbers() {
  if (!docOpen || mode !== "editor") return;
  try {
    const pageCount = editor.getCountPages() | 0;
    forEachPageInTransaction((doc, nPage, pageW, pageH, rotAngle) => {
      addPageFreeText(doc, nPage, pageW, pageH, rotAngle, `${nPage + 1} / ${pageCount}`, {
        width: 90, height: 24, anchor: "bottom-center", fontSize: 10, color: [0, 0, 0], opacity: 1,
      });
    });
    refreshHistoryButtons();
    setStatus(`Seitenzahlen auf ${pageCount} Seite(n) eingefügt.`);
  } catch (e) {
    console.error("Seitenzahlen fehlgeschlagen:", e);
    setStatus(`Seitenzahlen fehlgeschlagen: ${e.message}`);
  }
}

// Stirling-PDF/OmniTools "extract images": pull every embedded picture out of
// the document as its own file, de-duplicating identical images (e.g. a logo
// repeated on every page).
//
// A page's pictures only show up in GetPageInfo(nPage).drawings once that
// page has been "recognized" — the same lazy conversion the "Text" tool
// triggers via CPDFDoc.EditPage() (scans the raw page content into editable
// drawing objects). A freshly opened PDF has no page recognized yet, so this
// force-recognizes every not-yet-recognized page first (same effect as
// clicking "Text" once per page) — undoable like any other edit, same as
// clicking through pages manually would be.
function extractEmbeddedImages() {
  if (!docOpen || mode !== "editor") return;
  try {
    const doc = editor.getPDFDoc();
    const pageCount = doc.GetPagesCount();
    const allIndexes = Array.from({ length: pageCount }, (_, i) => i);

    // 4th arg (Additional) is required here: Document_Is_SelectionLocked's
    // historydescription_Pdf_EditPage case reads it straight as the page
    // index list to lock-check (CheckPages(fn, aSelectedPagesIdx)) — omitting
    // it crashes reading .length of undefined before the action even runs.
    const extractEntry = { pointsBefore: historyPointCount(), wasDirty: docDirty };
    doc.DoAction(function () {
      for (let nPage = 0; nPage < pageCount; nPage++) {
        if (!doc.Viewer.file.pages[nPage].isRecognized) {
          doc.EditPage(nPage);
        }
      }
    }, window.AscDFH.historydescription_Pdf_EditPage, doc, allIndexes);
    refreshHistoryButtons();

    const seen = new Set();
    const images = [];
    for (let nPage = 0; nPage < pageCount; nPage++) {
      const pageInfo = doc.GetPageInfo(nPage);
      if (!pageInfo || !Array.isArray(pageInfo.drawings)) continue;
      for (const drawing of pageInfo.drawings) {
        if (!drawing.IsImage || !drawing.IsImage()) continue;
        const blipFill = drawing.getBlipFill && drawing.getBlipFill();
        const dataUrl = blipFill && blipFill.getBase64RasterImageId(false, true);
        if (!dataUrl || seen.has(dataUrl)) continue;
        seen.add(dataUrl);
        images.push(dataUrl);
      }
    }
    if (!images.length) {
      rollbackIfPureRecognition(extractEntry);
      setStatus("Keine eingebetteten Bilder in diesem Dokument gefunden.");
      return;
    }
    const base = lastName.replace(/\.pdf$/i, "");
    images.forEach((dataUrl, i) => {
      const ext = (dataUrl.match(/^data:image\/(\w+);/) || [, "png"])[1];
      downloadDataUrl(dataUrl, `${base}-Bild-${i + 1}.${ext}`);
    });
    rollbackIfPureRecognition(extractEntry); // reading images must not leave pages recognized
    setStatus(`${images.length} Bild(er) extrahiert.`);
  } catch (e) {
    console.error("Bilder extrahieren fehlgeschlagen:", e);
    setStatus(`Bilder extrahieren fehlgeschlagen: ${e.message}`);
  }
}

// Stirling-PDF "PDF to Image": render a chosen page range to standalone PNG
// files at a chosen DPI, via the same offscreen renderer the app already uses
// for print (GetPrintPage renders any page without navigating to it first).
async function exportPagesAsImages() {
  if (!docOpen || mode !== "editor") return;
  const pageCount = editor.getCountPages() | 0;
  const spec = await showPromptDialog(
    `Welche Seiten sollen als Bilder exportiert werden?\nz.B. "1-3,5" — Dokument hat ${pageCount} Seite(n).`,
    `1-${pageCount}`
  );
  if (!spec) return;

  let indexes;
  try {
    indexes = parsePageRangeSpec(spec, pageCount);
  } catch (e) {
    setStatus(`Bildexport fehlgeschlagen: ${e.message}`);
    return;
  }

  const dpiStr = await showPromptDialog("Auflösung in DPI (z.B. 150):", "150");
  const dpi = Math.max(50, Math.min(600, parseInt(dpiStr, 10) || 150));

  try {
    const doc = editor.getPDFDoc();
    const r = renderer();
    if (!r || typeof r.GetPrintPage !== "function") {
      setStatus("Bildexport nicht verfügbar (Renderer fehlt).");
      return;
    }
    const base = lastName.replace(/\.pdf$/i, "");
    setStatus(`Erzeuge ${indexes.length} Bild(er) …`);
    for (const nPage of indexes) {
      const widthPx = Math.round(doc.GetPageWidthMM(nPage) / 25.4 * dpi);
      const heightPx = Math.round(doc.GetPageHeightMM(nPage) / 25.4 * dpi);
      const canvas = r.GetPrintPage(nPage, widthPx, heightPx, window.AscPDF.PRINT_CONTENT_TYPES.docAndMarkups);
      downloadDataUrl(canvas.toDataURL("image/png"), `${base}-Seite-${nPage + 1}.png`);
      await new Promise((res) => setTimeout(res, 60));
    }
    setStatus(`${indexes.length} Seite(n) als PNG exportiert.`);
  } catch (e) {
    console.error("Bildexport fehlgeschlagen:", e);
    setStatus(`Bildexport fehlgeschlagen: ${e.message}`);
  }
}

// The two renderers use different zoom units: the fallback CViewer speaks
// PERCENT (getZoom()/setZoom(110)), the editor's CHtmlPage a FACTOR
// (.zoom = 1.1, setZoom(1.1)). Feeding percent into CHtmlPage.setZoom zoomed
// 110-fold and crashed the WASM rasterizer ("memory access out of bounds").
function getZoomPercent(r) {
  if (typeof r.getZoom === "function") return Math.round(r.getZoom());
  return Math.round((typeof r.zoom === "number" ? r.zoom : 1) * 100);
}

function setZoomPercent(r, percent) {
  r.setZoom(typeof r.getZoom === "function" ? percent : percent / 100);
}

function stepZoom(dir) {
  const r = renderer();
  if (!r) return;
  const z = getZoomPercent(r);
  const next = dir > 0 ? ZOOM_STEPS.find((v) => v > z) : [...ZOOM_STEPS].reverse().find((v) => v < z);
  if (next) setZoomPercent(r, next);
  setStatus(`Zoom: ${next || z} %`);
}

// ── Toolbar state ─────────────────────────────────────────────────────────
function setActiveTool(name) {
  if (activeTool === "edit-text" && name !== "edit-text" && !editableMarkerTool) {
    leavePageEditFocus();
  }
  activeTool = name;
  for (const btn of document.querySelectorAll(".toolbar .tool")) {
    const t = btn.getAttribute("data-tool");
    const isActive = (name === t) || (name === "marker:Highlight" && t === "highlight")
      || (name === "marker:Underline" && t === "underline")
      || (name === "marker:Strikeout" && t === "strikeout");
    btn.classList.toggle("active", isActive);
  }
}

function setToolEnabled(tool, on) {
  const btn = document.querySelector(`[data-tool="${tool}"]`);
  if (btn) btn.disabled = !on;
}

// Tools available once a document is open in editor mode.
const EDITOR_TOOLS = [
  "undo", "redo", "select", "hand", "edit-text", "textbox", "highlight", "underline",
  "strikeout", "shape", "shape-ellipse", "shape-line", "shape-arrow", "ink", "comment",
  "image", "signature", "page-add", "page-remove", "page-remove-range", "page-move",
  "pdf-append", "pdf-extract", "rotate-left", "rotate-right", "rotate-all", "zoom-out", "zoom-in",
  "fit-width", "fit-page", "form-fill", "watermark", "page-numbers", "extract-images",
  "pages-to-images", "ocr", "ocr-txt",
];

function enableEditing(on) {
  el("btn-save").disabled = !(on && mode === "editor");
  el("btn-print").disabled = !(on && mode === "editor");
  el("btn-mail").disabled = !(on && mode === "editor");
  for (const tool of EDITOR_TOOLS) {
    // in viewer fallback, only view tools are usable
    const viewerOk = ["zoom-out", "zoom-in", "fit-width", "fit-page"].includes(tool);
    setToolEnabled(tool, on && (mode === "editor" || viewerOk));
  }
  setFormatEnabled(on && mode === "editor");
  if (on) setActiveTool("select");
}

function waitFor(predicate, timeoutMs, errMsg) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    (function poll() {
      let ok = false;
      try { ok = predicate(); } catch { ok = false; }
      if (ok) return resolve();
      if (Date.now() - start > timeoutMs) return reject(new Error(errMsg || "Timeout"));
      setTimeout(poll, 50);
    })();
  });
}

// ── Wiring ────────────────────────────────────────────────────────────────
function wireUi() {
  el("file-input").addEventListener("change", (e) => onFileChosen(e.target.files[0]));
  // Desktop: route "Öffnen" through the native dialog in the main process so
  // the recent-files list gets a real file path to reopen from.
  if (window.desktop && typeof window.desktop.openPdfDialog === "function") {
    const openLabel = document.querySelector('label[for="file-input"]');
    if (openLabel) {
      openLabel.addEventListener("click", (e) => {
        e.preventDefault();
        window.desktop.openPdfDialog();
      });
    }
  }
  el("btn-save").addEventListener("click", saveDocument);
  el("btn-print").addEventListener("click", printDocument);
  el("btn-mail").addEventListener("click", emailDocument);
  wireFormatControls();
  wireSearchBar({
    getEditor: () => editor,
    canSearch: () => mode === "editor" && docOpen,
    refocusEditor,
  });
  wirePromptDialog(refocusEditor);
  wireStatusBar({
    getRenderer: renderer,
    isDocOpen: () => docOpen,
    setZoomPercent: (percent) => { const r = renderer(); if (r) setZoomPercent(r, percent); },
    refocusEditor,
  });
  wireSignatureDialog({ insertImageDataUrl, refocusEditor, setStatus });
  initRecovery({
    isDirty: () => docDirty,
    isDocOpen: () => docOpen,
    collectPdfBytes,
    getDocName: () => lastName,
    openArrayBuffer,
    markSaved: () => markDirty(false),
  });
  renderRecentFiles();
  initMarkerColor();
  initOcr({
    getEditor: () => editor, isDocOpen: () => docOpen, showPromptDialog,
    parsePageRangeSpec, getDocName: () => lastName, setStatus, renderer,
    collectPdfBytes, openArrayBuffer, loadPdfLib,
    markClean: () => markDirty(false),
  });

  // ONLYOFFICE's text-input layer (common/text_input2.js) installs a global
  // document "focus" listener: whenever DOM focus lands on an element it does
  // not recognise as editable, it treats that as "left the editor" and disables
  // key-event capture. Our plain <button> toolbar steals focus on click, which
  // silently kills typing in text boxes/annotations afterwards — the box gets
  // created (mouse events still work), but no keystroke ever reaches it.
  // Standard fix (every WYSIWYG toolbar does this): stop the click's implicit
  // focus shift on mousedown. The click event itself still fires normally.
  el("toolbar").addEventListener("mousedown", (e) => {
    if (e.target.closest("button, summary")) e.preventDefault();
  });

  // Run after the SDK's canvas handler. The first click may need to enter an
  // OCR-created drawing's text state; later clicks are left entirely native so
  // the caret can be placed character by character. Editable marker formatting
  // is applied after a drag has produced selection quads.
  el("editor_sdk").addEventListener("click", () => {
    setTimeout(activateSelectedTextForTyping, 0);
  }, true);
  el("editor_sdk").addEventListener("mouseup", () => {
    if (editableMarkerTool) setTimeout(applyEditableMarkerSelection, 0);
  }, true);

  for (const btn of document.querySelectorAll("[data-tool]")) {
    const tool = btn.getAttribute("data-tool");
    btn.addEventListener("click", (event) => {
      const handler = TOOL_HANDLERS[tool];
      if (!handler) return;
      try { handler(event); } catch (e) {
        console.error(`Tool '${tool}' fehlgeschlagen:`, e);
        setStatus(`Aktion „${tool}" fehlgeschlagen: ${e.message}`);
      }
    });
  }

  const host = document.querySelector(".viewer-host");

  // Strg+Mausrad = Zoom (standard PDF-viewer behaviour). Capture phase +
  // passive:false so we beat the engine's own scroll handling and may call
  // preventDefault (which also stops the browser's page zoom).
  host.addEventListener("wheel", (e) => {
    if (!(e.ctrlKey || e.metaKey) || !docOpen) return;
    e.preventDefault();
    e.stopPropagation();
    stepZoom(e.deltaY < 0 ? 1 : -1);
  }, { capture: true, passive: false });

  const workspace = document.querySelector(".workspace");
  const dropOverlay = el("pdf-drop-overlay");
  const dropMessage = el("pdf-drop-message");
  const isFileDrag = (event) =>
    event.dataTransfer && Array.from(event.dataTransfer.types || []).includes("Files");
  const hideDropOverlay = () => { dropOverlay.hidden = true; };

  workspace.addEventListener("dragenter", (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    dropMessage.textContent = docOpen
      ? "PDF hier ablegen, um sie an das Dokument anzuhängen."
      : "PDF hier ablegen, um sie zu öffnen.";
    dropOverlay.hidden = false;
  });
  workspace.addEventListener("dragover", (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  workspace.addEventListener("dragleave", (e) => {
    if (!workspace.contains(e.relatedTarget)) hideDropOverlay();
  });
  workspace.addEventListener("drop", (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    hideDropOverlay();

    const files = Array.from(e.dataTransfer.files || []);
    const file = files.find((candidate) =>
      candidate.type === "application/pdf" || /\.pdf$/i.test(candidate.name));
    if (!file) {
      setStatus("Bitte eine PDF-Datei ablegen.");
      return;
    }
    if (docOpen && mode === "editor") {
      appendPdfFile(file);
    } else if (!docOpen) {
      onFileChosen(file);
    } else {
      setStatus("PDF anhängen ist im schreibgeschützten Modus nicht verfügbar.");
    }
  });
  window.addEventListener("dragend", hideDropOverlay);
  window.addEventListener("blur", hideDropOverlay);

  window.addEventListener("resize", () => {
    if (mode === "editor" && editor && editor.WordControl) {
      try { editor.WordControl.OnResize(true); } catch { /* ignore */ }
    }
  });

  // Keyboard shortcuts. Capture phase so they win over the engine's own key
  // handling; undo/redo (Strg+Z/Y) is left to the engine.
  window.addEventListener("keydown", (e) => {
    if (!e.ctrlKey && !e.metaKey && !e.altKey
        && (e.key === "Delete" || e.key === "Backspace")
        && editableMarkerTool && clearEditableMarkerSelection()) {
      e.preventDefault();
      e.stopPropagation();
      return;
    }

    if (e.key === "F3") {
      e.preventDefault();
      e.stopPropagation();
      searchStep(e.shiftKey ? -1 : 1);
      return;
    }
    if (!(e.ctrlKey || e.metaKey)) return;
    const k = e.key.toLowerCase();
    if (k === "s") {
      e.preventDefault();
      e.stopPropagation();
      saveDocument();
    } else if (k === "p") {
      e.preventDefault();
      e.stopPropagation();
      printDocument();
    } else if (k === "o") {
      e.preventDefault();
      e.stopPropagation();
      if (window.desktop && typeof window.desktop.openPdfDialog === "function") window.desktop.openPdfDialog();
      else el("file-input").click();
    } else if (k === "f") {
      e.preventDefault();
      e.stopPropagation();
      openSearchBar();
    }
  }, true);

  // Warn before closing with unsaved changes. In the browser the native
  // beforeunload prompt handles it; in Electron (no native prompt) block the
  // close once and ask via confirm().
  window.addEventListener("beforeunload", (e) => {
    if (!docDirty) return;
    if (window.desktop) {
      e.preventDefault();
      e.returnValue = false;
      setTimeout(() => {
        if (window.confirm(`„${lastName}" hat ungespeicherte Änderungen. Trotzdem schließen?`)) {
          docDirty = false;
          clearRecoverySnapshot();
          window.close();
        }
      });
    } else {
      e.preventDefault();
      e.returnValue = "";
    }
  });
}

wireUi();
restoreTextCommitTransition();
initEditor().catch((err) => {
  editorErrorMsg = (err && err.message) ? err.message : String(err);
  console.error("Editor-Bootstrap fehlgeschlagen:", err);
  setStatus(`Editor konnte nicht geladen werden: ${editorErrorMsg} — Wechsel in den Nur-Lese-Modus …`);
  initViewerFallback().catch((err2) => {
    console.error(err2);
    setStatus(`Fehler beim Laden der Engine: ${err2.message}`);
  });
});
