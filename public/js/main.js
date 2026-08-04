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

// Format controls (selects, color picker) take DOM focus; hand it back to the
// editor afterwards so typing keeps working (text_input2 disables key capture
// when focus leaves the editor).
function refocusEditor() {
  try { editor.asc_enableKeyEvents(true); } catch { /* best effort */ }
}

// OCR-rebuilt words arrive as individual drawing objects. A normal click can
// stop at the object's resize frame, so replay it through ONLYOFFICE's native
// text-hit path. That path installs selection.textSelection, starts
// TextAddState and places the caret at the clicked character.
function supportsTextHitActivation(object) {
  if (!object || !object.invertTransformText) return false;
  const requiredMethods = [
    "Get_Id",
    "getDocContent",
    "getProtectionLockText",
    "select",
    "selectionSetStart",
    "updateSelectionState",
  ];
  return requiredMethods.every((name) => typeof object[name] === "function")
    && typeof object.invertTransformText.TransformPointX === "function"
    && typeof object.invertTransformText.TransformPointY === "function";
}

function resetFailedTextHitActivation(controller) {
  if (!controller) return;
  try {
    // Passing true avoids asking a partially installed text object for its
    // document content while the selection is being discarded.
    if (typeof controller.resetSelection === "function") controller.resetSelection(true);
    else if (controller.selection) controller.selection.textSelection = null;
  } catch {
    if (controller.selection) controller.selection.textSelection = null;
  }
  try {
    if (window.AscFormat && typeof window.AscFormat.NullState === "function"
        && typeof controller.changeCurrentState === "function") {
      controller.changeCurrentState(new window.AscFormat.NullState(controller));
    }
  } catch { /* best effort */ }
  try {
    if (typeof controller.updateSelectionState === "function") controller.updateSelectionState();
  } catch { /* selection has already been cleared */ }
}

function activateSelectedTextForTyping() {
  if ((activeTool !== "edit-text" && !editableMarkerTool) || !editor) return;
  let controller = null;
  try {
    const doc = editor.getPDFDoc();
    const active = doc.GetActiveObject();
    if (!active || !active.IsDrawing || !active.IsDrawing()) return;

    controller = doc.GetController();
    // Native hit handling already put the drawing into text-edit state. Replaying
    // handleTextHit in that case can reuse a double-click count and select the
    // whole word/object, which prevents precise character-by-character edits.
    if (controller && controller.selection
        && controller.selection.textSelection === active) {
      refocusEditor();
      return;
    }

    const viewer = renderer();
    const mouse = window.AscCommon && window.AscCommon.global_mouseEvent;
    if (!controller || typeof controller.handleTextHit !== "function"
        || !viewer || typeof viewer.getPageByCoords2 !== "function" || !mouse) return;

    // A scanned page's background image is also a drawing, but it is not a
    // text shape. handleTextHit mutates selection.textSelection before calling
    // selectionSetStart, so passing such an object corrupts the controller and
    // causes every later repaint to throw in updateSelectionState.
    if (!supportsTextHitActivation(active)) return;

    let point = viewer.getPageByCoords2(mouse.X, mouse.Y);
    if (!point) return;
    const page = typeof active.GetPage === "function" ? active.GetPage() : point.index;
    if (page !== point.index && window.AscPDF && typeof window.AscPDF.ConvertCoordsToAnotherPage === "function") {
      point = window.AscPDF.ConvertCoordsToAnotherPage(point.x, point.y, point.index, page);
      point.index = page;
    }

    controller.handleTextHit(active, mouse, point.x, point.y, null, page, false);
    controller.updateSelectionState();
    renderer() && renderer().onUpdateOverlay();
    doc.UpdateInterface();
    refocusEditor();
  } catch (error) {
    resetFailedTextHitActivation(controller);
    console.warn("Textobjekt konnte nicht aktiviert werden:", error);
  }
}

let textboxBorderWidth = 0;
let textboxBorderColorHex = "#000000";

function isFreeTextAnnotation(object) {
  if (!object) return false;
  try {
    if (typeof object.IsFreeText === "function" && object.IsFreeText()) return true;
    return typeof object.GetType === "function" && object.GetType() === annotType("FreeText");
  } catch {
    return false;
  }
}

function activeFreeTextAnnotation() {
  if (!editor || !docOpen) return null;
  try {
    const active = editor.getPDFDoc().GetActiveObject();
    return isFreeTextAnnotation(active) ? active : null;
  } catch {
    return null;
  }
}

function pdfBorderColor(hex = textboxBorderColorHex) {
  return [
    parseInt(hex.slice(1, 3), 16) / 255,
    parseInt(hex.slice(3, 5), 16) / 255,
    parseInt(hex.slice(5, 7), 16) / 255,
  ];
}

function borderColorHex(color) {
  if (!Array.isArray(color) || color.length < 3) return "#000000";
  const scale = Math.max(...color.slice(0, 3)) <= 1 ? 255 : 1;
  return "#" + color.slice(0, 3).map((value) =>
    Math.max(0, Math.min(255, Math.round(value * scale))).toString(16).padStart(2, "0")
  ).join("");
}

function applyTextboxBorder(annotation, applyWidth = true, applyColor = true) {
  if (!isFreeTextAnnotation(annotation)) return false;
  try {
    if (applyColor && typeof annotation.SetBorderColor === "function") {
      annotation.SetBorderColor(pdfBorderColor());
    }
    if (applyWidth && typeof annotation.SetBorderWidth === "function") {
      annotation.SetBorderWidth(textboxBorderWidth);
    }
    editor.getPDFDoc().UpdateInterface();
    renderer() && renderer().onUpdateOverlay();
    refreshHistoryButtons();
    return true;
  } catch (e) {
    console.warn("Textfeldrahmen setzen fehlgeschlagen:", e);
    return false;
  }
}

function syncTextboxBorderControls() {
  const annotation = activeFreeTextAnnotation();
  if (!annotation) return;
  try {
    const width = Number(annotation.GetBorderWidth && annotation.GetBorderWidth()) || 0;
    const widthSelect = el("textbox-border-width");
    if (widthSelect) {
      if (![...widthSelect.options].some((option) => Number(option.value) === width)) {
        const option = document.createElement("option");
        option.value = String(width);
        option.textContent = `${String(width).replace(".", ",")} pt`;
        widthSelect.appendChild(option);
      }
      widthSelect.value = String(width);
    }
    const color = annotation.GetBorderColor && annotation.GetBorderColor();
    const colorInput = el("textbox-border-color");
    if (colorInput && color) colorInput.value = borderColorHex(color);
  } catch { /* leave the current defaults visible */ }
}

function applyDefaultsToNewTextbox(previousAnnotations) {
  let attempts = 0;
  const apply = () => {
    let created = null;
    try {
      const doc = editor.getPDFDoc();
      created = (doc.annots || []).find((annotation) =>
        !previousAnnotations.has(annotation) && isFreeTextAnnotation(annotation));
      if (!created) {
        const active = doc.GetActiveObject();
        if (!previousAnnotations.has(active) && isFreeTextAnnotation(active)) created = active;
      }
    } catch { /* annotation is still being created */ }
    if (created) {
      applyTextboxBorder(created);
      return;
    }
    if (++attempts < 40) setTimeout(apply, 50);
  };
  apply();
}

async function initTextboxBorderControls() {
  const savedWidth = Number(await storeGet("textbox-border-width"));
  const savedColor = await storeGet("textbox-border-color");
  if ([0, 0.5, 1, 2, 3].includes(savedWidth)) textboxBorderWidth = savedWidth;
  if (typeof savedColor === "string" && /^#[0-9a-fA-F]{6}$/.test(savedColor)) {
    textboxBorderColorHex = savedColor;
  }
  el("textbox-border-width").value = String(textboxBorderWidth);
  el("textbox-border-color").value = textboxBorderColorHex;
}

function wireFormatControls() {
  const fontSel = el("text-font-family"), sizeInp = el("text-font-size");
  const boldBtn = el("text-bold"), italicBtn = el("text-italic"), colorInp = el("text-color");
  const borderWidthSel = el("textbox-border-width"), borderColorInp = el("textbox-border-color");
  if (!fontSel) return;

  fontSel.addEventListener("change", () => {
    try { editor.put_TextPrFontName(fontSel.value); } catch (e) { console.warn("Schriftart setzen fehlgeschlagen:", e); }
    refocusEditor();
  });
  sizeInp.addEventListener("change", () => {
    const size = Math.max(6, Math.min(96, parseFloat(sizeInp.value) || 12));
    sizeInp.value = String(size);
    try { editor.put_TextPrFontSize(size); } catch (e) { console.warn("Schriftgröße setzen fehlgeschlagen:", e); }
    refocusEditor();
  });
  boldBtn.addEventListener("click", () => {
    try { editor.put_TextPrBold(!fmtState.bold); } catch (e) { console.warn("Fett fehlgeschlagen:", e); }
    refocusEditor();
  });
  italicBtn.addEventListener("click", () => {
    try { editor.put_TextPrItalic(!fmtState.italic); } catch (e) { console.warn("Kursiv fehlgeschlagen:", e); }
    refocusEditor();
  });
  colorInp.addEventListener("change", () => {
    try {
      const hex = colorInp.value;
      const color = new window.Asc.asc_CColor(
        parseInt(hex.slice(1, 3), 16),
        parseInt(hex.slice(3, 5), 16),
        parseInt(hex.slice(5, 7), 16)
      );
      editor.put_TextColor(color);
    } catch (e) { console.warn("Textfarbe fehlgeschlagen:", e); }
    refocusEditor();
  });
  borderWidthSel.addEventListener("change", () => {
    textboxBorderWidth = Math.max(0, Math.min(12, Number(borderWidthSel.value) || 0));
    storeSet("textbox-border-width", textboxBorderWidth);
    applyTextboxBorder(activeFreeTextAnnotation(), true, false);
    refocusEditor();
  });
  borderColorInp.addEventListener("change", () => {
    textboxBorderColorHex = borderColorInp.value;
    storeSet("textbox-border-color", textboxBorderColorHex);
    applyTextboxBorder(activeFreeTextAnnotation(), false, true);
    refocusEditor();
  });
}

function setFormatEnabled(on) {
  for (const id of ["text-font-family", "text-font-size", "text-bold", "text-italic", "text-color",
    "textbox-border-width", "textbox-border-color", "marker-color"]) {
    const node = el(id);
    if (node) node.disabled = !on;
  }
}

function setToggleState(id, active) {
  const btn = el(id);
  if (!btn) return;
  btn.classList.toggle("active", active);
  btn.setAttribute("aria-pressed", active ? "true" : "false");
}

function registerFormatCallbacks(on) {
  on("asc_onBold", (v) => {
    fmtState.bold = !!v;
    setToggleState("text-bold", fmtState.bold);
  });
  on("asc_onItalic", (v) => {
    fmtState.italic = !!v;
    setToggleState("text-italic", fmtState.italic);
  });
  on("asc_onFontFamily", (f) => {
    const name = f && (typeof f.asc_getName === "function" ? f.asc_getName() : f.Name);
    const fontSel = el("text-font-family");
    if (!name || !fontSel || name.startsWith("Embedded: ") || document.activeElement === fontSel) return;
    // fonts outside the curated list (e.g. reported by the cursor position)
    // get an option on the fly so the dropdown can display them
    if (![...fontSel.options].some((o) => o.value === name)) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      fontSel.appendChild(opt);
    }
    fontSel.value = name;
  });
  on("asc_onFontSize", (size) => {
    const sizeInp = el("text-font-size");
    if (typeof size === "number" && size > 0 && sizeInp && document.activeElement !== sizeInp) {
      sizeInp.value = String(Math.round(size * 10) / 10);
    }
  });
}

function registerEditorCallbacks() {
  const on = (name, cb) => {
    try { editor.asc_registerCallback(name, cb); } catch { /* optional event */ }
  };

  on("asc_onError", (id, level) => {
    // Level.Critical === -1, Level.NoCritical === 0. Non-critical errors (e.g.
    // the optional ChartStyles.js 404, id -24) must not clobber the status or
    // alarm the user — log them and carry on.
    const critical = (level === -1);
    if (critical) {
      console.error("[editor] asc_onError (critical)", id, level);
      setStatus(`Editor-Fehler (${id}).`);
    } else {
      console.warn("[editor] asc_onError (non-critical, ignored)", id, level);
    }
  });
  on("asc_onDocumentContentReady", () => {
    docOpen = true;
    resetLongActionCounter();
    suppressFormDesignLabels();
    enableEditing(true);
    setStatus(`„${lastName}" geöffnet — bereit zum Bearbeiten.`);
    ensureEditorThumbnails();
    refreshHistoryButtons();
    preloadFallbackFonts();
    preloadFieldFonts();
    markDirty(false);
    if (pendingKeepDirty) {
      pendingKeepDirty = false;
      markDirty(true); // the committed text edits are not saved to disk yet
      setStatus(`Textänderungen übernommen — „${lastName}" ist bereit zum Bearbeiten (noch nicht gespeichert).`);
    }
    updateTitle();
    // default to text selection (the engine's open path forces hand/pan
    // mode, which blocks drag-select and with it markers and Strg+C)
    setViewerTargetType("select");
    installDocMouseGuard();
    updatePageCount(editor.getCountPages ? editor.getCountPages() : 0);
    updateCurrentPage(editor.getCurrentPage ? editor.getCurrentPage() : 0);
    try { updateZoomDisplay(getZoomPercent(renderer())); } catch { /* not ready */ }
    setStatusControlsVisible(true);
  });
  on("asc_onCountPages", (n) => updatePageCount(n));
  on("asc_onCurrentPage", (n) => {
    updateCurrentPage(n);
    if (activeTool === "edit-text") scheduleCurrentPageTextEditing(n);
  });
  on("asc_onZoomChange", (percent) => updateZoomDisplay(percent));
  on("asc_onCanUndo", (v) => {
    setToolEnabled("undo", docOpen && !!v);
    if (docOpen && v) {
      markDirty(true); // any undoable change = unsaved changes
      if (activeTool === "edit-text" && editModeEntry) {
        const fresh = newActivePoints(editModeEntry);
        const latest = fresh && fresh[fresh.length - 1];
        if (latest && latest.Description !== window.AscDFH.historydescription_Pdf_EditPage) {
          markCurrentTextPageEdited();
        }
      }
    }
  });
  on("asc_onCanRedo", (v) => setToolEnabled("redo", docOpen && !!v));
  on("asc_onMarkerFormatChanged", (type, isOn) => {
    // keep the toolbar's active highlight in sync if the engine toggles it off
    if (!isOn) clearActiveMarkerTools();
  });
  registerFormatCallbacks(on);
}

// Preload the glyph-fallback fonts as soon as a document is open. The
// per-glyph fallback (textmeasurer GetFontBySymbol → CFontLoaderBySymbol →
// FontPickerByCharacter ranges → CFontInfo.LoadFont) only works if the target
// font FILE is already in memory: if it isn't, the lookup returns null (at
// best kicking an async load with no repaint hook) and the glyph stays a
// .notdef box forever. Loading through LoadDocumentFonts2 also gives us the
// proper end-callback → repaint pipeline.
function preloadFallbackFonts() {
  try {
    const loader = window.AscCommon && window.AscCommon.g_font_loader;
    if (!loader || typeof loader.LoadDocumentFonts2 !== "function" || loader.isWorking()) return;
    const families = [
      "Liberation Sans", "Liberation Serif", "Liberation Mono",
      "DejaVu Sans", "DejaVu Serif", "FreeSans", "FreeSerif",
      "Symbola", "Carlito", "Caladea",
    ].map((name) => ({ name }));
    console.log("[fonts] preloading fallback fonts …");
    loader.LoadDocumentFonts2(families, undefined, function () {
      console.log("[fonts] fallback fonts preloaded");
      // Also register them in the PDF document's loadedFonts list. Text entry
      // into form fields is gated by checkFieldFont, which requires the field
      // font PLUS every font the character picker has resolved so far
      // (extendFonts) to be in that list — each entry it finds missing drops
      // one keystroke while it re-registers an already-loaded font. The
      // picker can only ever resolve to these preloaded families, so marking
      // them here makes that gate permanently pass.
      try {
        const doc = editor.getPDFDoc && editor.getPDFDoc();
        if (doc && Array.isArray(doc.loadedFonts)) {
          for (const f of families) {
            if (!doc.loadedFonts.includes(f.name)) doc.loadedFonts.push(f.name);
          }
        }
      } catch { /* best effort */ }
      try { const r = editor.getDocumentRenderer(); r && r.paint && r.paint(); } catch { /* ignore */ }
    });
  } catch (e) {
    console.warn("[fonts] preload fehlgeschlagen:", e);
  }
}

// The editor's own onDocumentContentReady creates a ThumbnailsControl into
// #thumbnails-list. Mirror that (idempotently) so Viewer.thumbnails is never
// null — asc_EditPage() and change repaints dereference it WITHOUT a null guard,
// so a missing thumbnails object crashes the first edit.
function ensureEditorThumbnails() {
  try {
    const r = editor.getDocumentRenderer && editor.getDocumentRenderer();
    if (!r || r.Thumbnails) return; // already created (by us or the editor)
    if (window.AscCommon && typeof window.AscCommon.ThumbnailsControl === "function"
        && document.getElementById("thumbnails-list")) {
      r.Thumbnails = new window.AscCommon.ThumbnailsControl("thumbnails-list");
      r.setThumbnailsControl(r.Thumbnails);
    }
  } catch (e) {
    console.warn("Thumbnails konnten nicht erstellt werden:", e);
  }
}

function refreshHistoryButtons() {
  try {
    if (typeof editor.asc_getCanUndo === "function") setToolEnabled("undo", docOpen && editor.asc_getCanUndo());
    if (typeof editor.asc_getCanRedo === "function") setToolEnabled("redo", docOpen && editor.asc_getCanRedo());
  } catch { /* ignore */ }
}

// Every sync_StartAction() must be paired with a matching sync_EndAction() to
// decrement IsLongActionCurrent back to 0; while it's non-zero, isLongAction()
// is true and CTextInputPrototype.onKeyDown() swallows every keystroke before
// any other processing (AscCommon.stopEvent + early return). Our offline open
// bypasses parts of the normal Start/End pairing (we mark server-wait gates
// complete directly rather than through the real network completion path), so
// if some action's matching End never ran, the counter can get stuck above 0
// forever — silently blocking all keyboard input with no error anywhere.
function resetLongActionCounter() {
  try {
    if (editor && typeof editor.IsLongActionCurrent === "number" && editor.IsLongActionCurrent !== 0) {
      console.warn(`[bootstrap] IsLongActionCurrent was ${editor.IsLongActionCurrent} (stuck long-action) — resetting to 0`);
      editor.IsLongActionCurrent = 0;
    }
  } catch (e) { console.warn("resetLongActionCounter fehlgeschlagen:", e); }
}

// ── Viewer fallback (read-only) ───────────────────────────────────────────
async function initViewerFallback() {
  setStatus("Editor nicht verfügbar — lade Lese-Engine …");
  await loadScript(VIEWER_BUNDLE);
  if (!(window.AscViewer && typeof window.AscViewer.CViewer === "function")) {
    throw new Error("Weder Editor noch Viewer-Engine verfügbar.");
  }
  el("editor_sdk").hidden = true;
  el("viewer-container").hidden = false;
  // Swap the thumbnail rails: the fallback viewer renders into #thumbnails.
  if (el("thumbnails-list")) el("thumbnails-list").hidden = true;
  if (el("thumbnails")) el("thumbnails").hidden = false;

  // The standalone viewer needs a #id_target_cursor element; create it here so
  // it never collides with the editor's own (the editor failed to load in this
  // branch, so #editor_sdk is empty).
  if (!el("id_target_cursor")) {
    const cur = document.createElement("div");
    cur.id = "id_target_cursor";
    cur.className = "cursor-target";
    document.querySelector(".viewer-host").appendChild(cur);
  }

  viewer = new window.AscViewer.CViewer("viewer-container", {
    sdkjsPath: SDKJS_PATH,
    fontsPath: FONTS_PATH,
  });
  thumbnails = viewer.createThumbnails("thumbnails");
  mode = "viewer";

  window.addEventListener("resize", () => {
    if (typeof window.AscViewer.checkApplicationScale === "function") window.AscViewer.checkApplicationScale();
    viewer && viewer.resize();
    thumbnails && thumbnails.resize();
  });
  setStatus("Nur-Lese-Modus: Bearbeiten ist in diesem Build nicht verfügbar.");
}

// ── Open / Save ───────────────────────────────────────────────────────────

// Opening a SECOND document into a live editor is not supported by the
// engine's own pipeline (content-ready never re-fires; upstream creates a
// fresh editor instance per document). Instead: stash the bytes in IndexedDB,
// reload the page (the beforeunload guard still protects unsaved changes)
// and open them in the fresh editor. Used by the file picker, drag&drop and
// the desktop "Öffnen mit" path alike.
const PENDING_STORE = "pending-open";

// The shared opener (storage.js) owns the DB version — opening with a stale
// explicit version here threw VersionError and broke the handover.
const pendingDb = openAppDb;

async function stashPendingOpenAndReload(bytes, name, keepDirty) {
  if (docDirty && !window.confirm(
    `„${lastName}" hat ungespeicherte Änderungen. Trotzdem „${name}" öffnen?`)) {
    return;
  }
  markDirty(false); // decision made — don't let the beforeunload guard interfere
  try {
    const db = await pendingDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(PENDING_STORE, "readwrite");
      tx.objectStore(PENDING_STORE).put({ name, bytes, keepDirty: !!keepDirty }, "file");
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
    if (textCommitReloadRequested) {
      try { sessionStorage.setItem(TEXT_COMMIT_TRANSITION_KEY, "1"); } catch { /* no session storage */ }
    }
    location.reload();
  } catch (e) {
    console.error("Zweites Dokument konnte nicht übergeben werden:", e);
    setStatus(`„${name}" konnte nicht geöffnet werden — bitte Seite neu laden.`);
  }
}

async function takePendingOpen() {
  try {
    const db = await pendingDb();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(PENDING_STORE, "readwrite");
      const store = tx.objectStore(PENDING_STORE);
      const get = store.get("file");
      get.onsuccess = () => {
        store.delete("file");
        resolve(get.result || null);
      };
      get.onerror = () => reject(get.error);
    });
  } catch {
    return null;
  }
}

function restoreTextCommitTransition() {
  try {
    textCommitReloadPending = sessionStorage.getItem(TEXT_COMMIT_TRANSITION_KEY) === "1";
    sessionStorage.removeItem(TEXT_COMMIT_TRANSITION_KEY);
  } catch { /* no session storage */ }
  if (!textCommitReloadPending) return;

  const title = document.querySelector("#placeholder .placeholder-title");
  const detail = document.querySelector("#placeholder .muted");
  if (title) title.textContent = "Textänderungen werden vorbereitet …";
  if (detail) detail.textContent = "Die Seite wird kurz aktualisiert, damit der bearbeitete Text vollständig markiert werden kann.";
  setStatus("Textänderungen werden vorbereitet …");
}

function openArrayBuffer(buf, name) {
  const bytes = new Uint8Array(buf);
  const magic = String.fromCharCode(...bytes.slice(0, 5));
  if (magic !== "%PDF-") {
    setStatus(`„${name}" ist keine gültige PDF-Datei.`);
    return;
  }
  if (docOpen && mode === "editor") {
    stashPendingOpenAndReload(bytes, name);
    return;
  }
  lastName = name;
  el("placeholder").style.display = "none";

  try {
    if (mode === "editor") {
      docOpen = false;
      editor.openDocument({ data: bytes });   // browser open: no server, no upload
      // Offline: there is no Document Server, so the two "wait for server" gates
      // that block _openDocumentEndCallback() never clear on their own. Mark
      // both complete; the editor's own onFileOpened → _openDocumentEndCallback
      // then fires asc_onDocumentContentReady through the proper pipeline (which
      // creates the thumbnails that asc_EditPage() dereferences unguarded).
      try {
        editor.ServerImagesWaitComplete = true;
        if (typeof editor.asyncServerIdEndLoaded === "function") {
          editor.asyncServerIdEndLoaded();           // sets ServerIdWaitComplete + nudges callback
        } else {
          editor.ServerIdWaitComplete = true;
          if (typeof editor._openDocumentEndCallback === "function") editor._openDocumentEndCallback();
        }
      } catch (e) { console.warn("Offline-Öffnen-Abschluss fehlgeschlagen:", e); }
      setStatus(`„${name}" wird geöffnet …`);
      scheduleOpenFallback(name);
    } else if (mode === "viewer") {
      viewer.open(buf);
      enableEditing(false);
      setToolEnabled("zoom-in", true); setToolEnabled("zoom-out", true);
      setToolEnabled("fit-width", true); setToolEnabled("fit-page", true);
      const why = editorErrorMsg ? ` — Editor-Fehler: ${editorErrorMsg}` : "";
      setStatus(`„${name}" geöffnet (Nur-Lese-Modus)${why}`);
    }
  } catch (e) {
    console.error("open() error:", e);
    setStatus(`„${name}" konnte nicht geöffnet werden: ${e.message}`);
  }
}

// Safety net for the offline open pipeline. Once the PDF document exists, keep
// nudging the *proper* guarded completion (_openDocumentEndCallback) until
// asc_onDocumentContentReady fires. Only if that never happens do we force the
// UI active — and even then we create thumbnails first, because asc_EditPage()
// dereferences Viewer.thumbnails without a null check.
function scheduleOpenFallback(name) {
  let tries = 0;
  const timer = setInterval(() => {
    if (docOpen) { clearInterval(timer); return; }
    tries++;
    let doc = null;
    try { doc = (typeof editor.getPDFDoc === "function") ? editor.getPDFDoc() : null; } catch { /* not ready */ }
    if (doc) {
      try {
        editor.ServerImagesWaitComplete = true;
        editor.ServerIdWaitComplete = true;
        if (typeof editor._openDocumentEndCallback === "function") editor._openDocumentEndCallback();
      } catch (e) { console.warn("_openDocumentEndCallback nudge fehlgeschlagen:", e); }
      if (docOpen) { clearInterval(timer); return; }
      // Content-ready can take a while (fonts finish loading first). Give the
      // natural path plenty of time before forcing.
      if (tries > 40) { // ~20s of nudging without the event → force the UI active
        clearInterval(timer);
        console.warn("[bootstrap] content-ready did not fire — forcing editor UI active");
        forceEnableEditing(name);
      }
    } else if (tries > 60) { // ~30s, document never materialised
      clearInterval(timer);
      console.warn("[bootstrap] open did not complete (no PDF document after 30s)");
      setStatus(`„${name}" konnte nicht vollständig geöffnet werden.`);
    }
  }, 500);
}

// Last-resort UI activation. Create the thumbnails control first (the editor
// does this in onDocumentContentReady, which by definition did not run here) so
// the unguarded asc_EditPage() dereference of Viewer.thumbnails cannot crash.
function forceEnableEditing(name) {
  ensureEditorThumbnails();
  docOpen = true;
  resetLongActionCounter();
  suppressFormDesignLabels();
  enableEditing(true);
  refreshHistoryButtons();
  setStatus(`„${name}" geöffnet — bereit zum Bearbeiten.`);
}

function onFileChosen(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => openArrayBuffer(reader.result, file.name);
  reader.onerror = () => setStatus("Datei konnte nicht gelesen werden.");
  reader.readAsArrayBuffer(file);
}

// ── Dirty state ───────────────────────────────────────────────────────────
// "Has the document been changed since the last save?" Driven by the
// engine's asc_onCanUndo events (any undoable change marks dirty) and
// cleared on successful save. Reflected in the window title.
let docDirty = false;
// set when a commit-reload carries unsaved edits across the reload
let pendingKeepDirty = false;

function markDirty(dirty) {
  if (docDirty === dirty) return;
  docDirty = dirty;
  updateTitle();
}

function updateTitle() {
  document.title = docOpen
    ? `${docDirty ? "• " : ""}${lastName} — Offline PDF Editor`
    : "Offline PDF Editor";
}

// Serialize the COMPLETE document to a finished PDF. CPDFDoc.GetPagesBinary
// runs the changes through the WASM serializer (SplitPages + SaveForSplit).
// NOTE: viewer.Save() is NOT that — it only returns the raw change-command
// stream meant as serializer input; an earlier "Speichern" wrote exactly that
// stream into .pdf files, which no PDF reader could open.
async function collectPdfBytes(forceRasterPages) {
  const doc = editor.getPDFDoc();
  try { doc.BlurActiveObject(); } catch { /* commits an active form field */ }
  const pageCount = editor.getCountPages() | 0;
  const indexes = Array.from({ length: pageCount }, (_, i) => i);
  const result = doc.GetPagesBinary(indexes, false);
  if (!result || !result.length || String.fromCharCode(...result.slice(0, 5)) !== "%PDF-") return null;
  let bytes = result instanceof Uint8Array ? result : new Uint8Array(result);
  bytes = await bakeShapesIntoPdf(bytes, indexes);
  // Text-mode edits can NOT be serialized by the engine's split writer (the
  // only PDF-producing WASM entry point — see patchSaveNoPageClear in the
  // build script): the saved bytes always carry the ORIGINAL page content.
  // So pages with real text edits are rasterized from the editor's own
  // renderer (which shows the edits correctly) and replace the page content,
  // plus an invisible OCR text layer so the text stays searchable/markable.
  const rasterPages = (forceRasterPages && forceRasterPages.length)
    ? forceRasterPages
    : editedTextPageIndexes();
  return rasterizePagesIntoPdf(bytes, rasterPages);
}

function editedTextPageIndexes() {
  const out = [];
  try {
    const v = editor.getDocumentRenderer();
    const n = editor.getCountPages() | 0;
    for (let i = 0; i < n; i++) {
      const page = v.file.pages[i];
      if (page && editedTextPageObjects.has(page)) out.push(i);
    }
  } catch { /* renderer not ready */ }
  return out;
}

function sessionHasRealEdits() {
  if (!editModeEntry) return false;
  const fresh = newActivePoints(editModeEntry);
  if (!fresh || !fresh.length) return false;
  return !fresh.every((p) => p && p.Description === window.AscDFH.historydescription_Pdf_EditPage);
}

const RASTER_DPI = 300; // match OCR resolution so rebuilt text selection stays precise

async function rasterizePagesIntoPdf(bytes, pages) {
  if (!pages || !pages.length) return bytes;
  await loadPdfLib();
  const { PDFDocument, PDFName, StandardFonts } = window.PDFLib;
  const pdf = await PDFDocument.load(bytes);
  const doc = editor.getPDFDoc();
  const r = renderer();
  const useOcr = await ocrStackAvailable();
  const font = useOcr ? await pdf.embedFont(StandardFonts.Helvetica) : null;

  for (const nPage of pages) {
    const page = pdf.getPage(nPage);
    if (!page) continue;
    const wPx = Math.round(doc.GetPageWidthMM(nPage) / 25.4 * RASTER_DPI);
    const hPx = Math.round(doc.GetPageHeightMM(nPage) / 25.4 * RASTER_DPI);
    // "doc" renders page content + drawings (the text edits) WITHOUT markup
    // annotations — those live on as real PDF annotations and must not be
    // burned into the bitmap twice.
    const canvas = r.GetPrintPage(nPage, wPx, hPx, window.AscPDF.PRINT_CONTENT_TYPES.doc);
    const png = await pdf.embedPng(canvas.toDataURL("image/png"));
    page.node.set(PDFName.of("Contents"), pdf.context.obj([]));
    page.drawImage(png, { x: 0, y: 0, width: page.getWidth(), height: page.getHeight() });
    if (useOcr) {
      try {
        setStatus(`Textebene für Seite ${nPage + 1} wird erzeugt (OCR) …`);
        const words = await recognizeCanvasWords(canvas);
        embedWordsOnPdfPage(page, words, wPx, font);
      } catch (e) {
        console.warn(`OCR-Textebene für Seite ${nPage + 1} fehlgeschlagen:`, e);
      }
    }
  }
  return pdf.save();
}

// The engine's split-based save stream (SaveForSplit → WASM SplitPages) has
// NO drawing serialization: user-drawn shapes (CShape page drawings) render
// in the editor but vanish silently from the saved bytes — the same reason
// the watermark is baked with pdf-lib. So collect every shape's geometry and
// draw it into the serialized PDF. Covers the four toolbar presets (rect,
// ellipse, line, lineWithArrow); free rotation is not re-applied (the
// toolbar has no rotate for shapes).
function collectShapeList(srcIndexes) {
  const doc = editor.getPDFDoc();
  const shapes = [];
  srcIndexes.forEach((nPage, outIndex) => {
    let drawings = [];
    try {
      const info = doc.GetPageInfo(nPage);
      drawings = (info && info.drawings) || [];
    } catch { return; }
    for (const d of drawings) {
      try {
        if (!d.IsShape || !d.IsShape() || !d.GetRect) continue;
        const rgba = (f) => { try { const c = f.fill.color.RGBA; return [c.R / 255, c.G / 255, c.B / 255]; } catch { return null; } };
        shapes.push({
          page: outIndex,
          rect: d.GetRect(), // [x1, y1, x2, y2] in pt, y measured from page TOP
          preset: d.getPresetGeom ? d.getPresetGeom()
            : (d.spPr && d.spPr.geometry && d.spPr.geometry.preset) || "rect",
          flipH: !!d.flipH,
          flipV: !!d.flipV,
          strokeWidthPt: d.pen && typeof d.pen.w === "number" ? d.pen.w / 12700 : 0.75,
          stroke: (d.pen && rgba(d.pen)) || [47 / 255, 84 / 255, 150 / 255],
          fill: d.brush ? rgba(d.brush) : null,
        });
      } catch { /* skip malformed drawing */ }
    }
  });
  return shapes;
}

async function bakeShapesIntoPdf(bytes, srcIndexes) {
  let shapes;
  try {
    shapes = collectShapeList(srcIndexes);
  } catch { return bytes; }
  if (!shapes.length) return bytes;

  await loadPdfLib();
  const { PDFDocument, rgb } = window.PDFLib;
  const pdf = await PDFDocument.load(bytes);
  const pages = pdf.getPages();

  for (const s of shapes) {
    const page = pages[s.page];
    if (!page) continue;
    const pageH = page.getHeight();
    const [x1, yTop1, x2, yTop2] = s.rect;
    const w = x2 - x1, h = yTop2 - yTop1;
    const stroke = rgb(...s.stroke);
    const thickness = Math.max(0.5, s.strokeWidthPt);

    if (s.preset === "line" || s.preset === "lineWithArrow") {
      // the xfrm box stores the drag's bounding box; flips encode direction
      let sx = s.flipH ? x2 : x1, ex = s.flipH ? x1 : x2;
      let syTop = s.flipV ? yTop2 : yTop1, eyTop = s.flipV ? yTop1 : yTop2;
      const start = { x: sx, y: pageH - syTop };
      const end = { x: ex, y: pageH - eyTop };
      page.drawLine({ start, end, thickness, color: stroke });
      if (s.preset === "lineWithArrow") {
        const ang = Math.atan2(end.y - start.y, end.x - start.x);
        const len = Math.max(6, thickness * 4);
        for (const da of [Math.PI - 0.5, Math.PI + 0.5]) {
          page.drawLine({
            start: end,
            end: { x: end.x + len * Math.cos(ang + da), y: end.y + len * Math.sin(ang + da) },
            thickness, color: stroke,
          });
        }
      }
    } else if (s.preset === "ellipse") {
      page.drawEllipse({
        x: x1 + w / 2, y: pageH - (yTop1 + h / 2),
        xScale: w / 2, yScale: h / 2,
        borderColor: stroke, borderWidth: thickness,
        color: s.fill ? rgb(...s.fill) : undefined,
      });
    } else { // rect and anything unknown: bounding box
      page.drawRectangle({
        x: x1, y: pageH - yTop2, width: w, height: h,
        borderColor: stroke, borderWidth: thickness,
        color: s.fill ? rgb(...s.fill) : undefined,
      });
    }
  }
  return pdf.save();
}

async function saveDocument() {
  if (mode !== "editor" || !docOpen) return;
  setStatus("PDF wird erzeugt …");
  try {
    const bytes = await collectPdfBytes();
    if (!bytes) {
      setStatus("Speichern fehlgeschlagen: keine gültigen PDF-Daten von der Engine.");
      return;
    }

    // Desktop app (Electron): native save dialog via the preload bridge,
    // suggesting the ORIGINAL file name. Web build: browser download with a
    // "-bearbeitet" suffix so the original is never silently shadowed.
    if (window.desktop && typeof window.desktop.savePdf === "function") {
      const res = await window.desktop.savePdf(bytes, lastName);
      if (res && res.saved) {
        markDirty(false);
        clearRecoverySnapshot();
        setStatus(`Gespeichert: ${res.path}`);
      } else if (res && res.error) {
        setStatus(`Speichern fehlgeschlagen: ${res.error}`);
      } else {
        setStatus("Speichern abgebrochen.");
      }
      return;
    }

    const outName = lastName.replace(/\.pdf$/i, "") + "-bearbeitet.pdf";
    downloadBytes(bytes, outName);
    markDirty(false);
    clearRecoverySnapshot();
    setStatus(`Gespeichert: „${outName}".`);
  } catch (e) {
    console.error("save() error:", e);
    setStatus(`Speichern fehlgeschlagen: ${e.message}`);
  }
}

// Drucken: the editor renders to canvases, so browser print of the page
// itself would not paginate. Instead hand the serialized PDF to something
// that CAN print it: desktop → temp file opened in the system's default PDF
// app; web → blob URL in a new tab (the browser's PDF viewer has printing).
async function printDocument() {
  if (mode !== "editor" || !docOpen) return;
  try {
    const bytes = await collectPdfBytes();
    if (!bytes) {
      setStatus("Drucken fehlgeschlagen: keine gültigen PDF-Daten von der Engine.");
      return;
    }
    if (window.desktop && typeof window.desktop.printPdf === "function") {
      const res = await window.desktop.printPdf(bytes, lastName);
      setStatus(res && res.ok
        ? "Zum Drucken in der Standard-PDF-Anwendung geöffnet."
        : `Drucken fehlgeschlagen: ${(res && res.error) || "unbekannt"}`);
      return;
    }
    const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
    const win = window.open(url, "_blank");
    setStatus(win
      ? "Druckansicht in neuem Tab geöffnet — dort Strg+P."
      : "Popup blockiert — bitte Popups für diese Seite erlauben.");
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (e) {
    console.error("print() error:", e);
    setStatus(`Drucken fehlgeschlagen: ${e.message}`);
  }
}

async function emailDocument() {
  if (mode !== "editor" || !docOpen) return;
  if (!window.desktop || typeof window.desktop.sendPdfByEmail !== "function") {
    setStatus("E-Mail mit PDF-Anhang ist nur in der Windows-Desktop-App verfügbar.");
    return;
  }

  setStatus("PDF für den Mailentwurf wird erzeugt …");
  try {
    const bytes = await collectPdfBytes();
    if (!bytes) {
      setStatus("E-Mail fehlgeschlagen: keine gültigen PDF-Daten von der Engine.");
      return;
    }

    setStatus("Mailentwurf wird im Standard-Mailprogramm geöffnet …");
    const res = await window.desktop.sendPdfByEmail(bytes, lastName);
    if (res && res.canceled) {
      setStatus("Mailentwurf geschlossen.");
    } else if (res && res.ok) {
      setStatus("PDF wurde an das Standard-Mailprogramm übergeben.");
    } else {
      setStatus(`E-Mail fehlgeschlagen: ${(res && res.error) || "unbekannt"}`);
    }
  } catch (e) {
    console.error("email() error:", e);
    setStatus(`E-Mail fehlgeschlagen: ${e.message}`);
  }
}

// ── Editing tools ─────────────────────────────────────────────────────────
function renderer() {
  if (mode === "editor" && editor && editor.getDocumentRenderer) return editor.getDocumentRenderer();
  return viewer; // fallback CViewer exposes the same setZoom/getZoom/setZoomMode
}
function annotType(name) { return (window.AscPDF && window.AscPDF.ANNOTATIONS_TYPES) ? window.AscPDF.ANNOTATIONS_TYPES[name] : undefined; }

// The viewer starts every document in HAND mode (setMouseLockMode(true) in
// its open path), and the engine's drag-to-select-text handler is explicitly
// gated on hand mode being OFF. The markers apply themselves to the text
// selection made while dragging — with hand mode on, that selection never
// happens, so the marker silently did nothing. Upstream's UI switches the
// "viewer target type" when tools change; do the same.
function setViewerTargetType(type) {
  try { editor.asc_setViewerTargetType(type); } catch { /* viewer not ready */ }
}

// Leaving an untouched Text page rolls back ONLYOFFICE's recognition point so
// the original viewer text layer remains selectable. Once the user has made a
// real edit, keep the recognized drawing model alive for the rest of the
// session. Rebuilding it through raster/OCR on every tool switch destroys the
// caret, run and selection identity and makes a second edit unreliable.
function leavePageEditFocus() {
  try { editor.getPDFDoc().BlurActiveObject(); } catch { /* nothing focused */ }
  const entry = editModeEntry;
  if (!entry) return;

  if (rollbackIfPureRecognition(entry)) {
    editModeEntry = null;
  }
}

// asc_EditPage "recognizes" the page: its content becomes drawing objects
// and the viewer layer permanently loses the text geometry — drag-to-select
// and markers then find no quads on that page, forever. Undoing the
// recognition restores everything. So: remember the history state when the
// Text tool is entered, and when the user leaves it WITHOUT real edits
// (every new history point is a Pdf_EditPage recognition point), roll the
// recognition back. Real text edits keep the object model — that page then
// intentionally stays in editing-oriented interaction.
let editModeEntry = null; // { pointsBefore, wasDirty }
const editedTextPageObjects = new WeakSet();
let editPageActivationTimer = null;

function currentPdfPageObject() {
  try {
    const view = renderer();
    const index = editor.getCurrentPage() | 0;
    return view && view.file && view.file.pages[index];
  } catch {
    return null;
  }
}

function markCurrentTextPageEdited() {
  const page = currentPdfPageObject();
  if (page) editedTextPageObjects.add(page);
}

function activateCurrentPageForTextEditing(expectedPage) {
  if (!docOpen || mode !== "editor" || activeTool !== "edit-text"
      || typeof editor.asc_EditPage !== "function") return;

  const pageIndex = editor.getCurrentPage() | 0;
  if (Number.isInteger(expectedPage) && expectedPage !== pageIndex) return;

  const page = currentPdfPageObject();
  if (!page || page.isRecognized) {
    refocusEditor();
    return;
  }

  try {
    markTextEditEntry();
    editor.asc_EditPage();
    setStatus(`Textbearbeitung für Seite ${pageIndex + 1} wird vorbereitet …`);
    requestAnimationFrame(() => refocusEditor());
  } catch (error) {
    console.warn(`Textbearbeitung für Seite ${pageIndex + 1} konnte nicht aktiviert werden:`, error);
    setStatus(`Textbearbeitung für Seite ${pageIndex + 1} konnte nicht aktiviert werden.`);
  }
}

function scheduleCurrentPageTextEditing(pageIndex, delay = 120) {
  if (editPageActivationTimer !== null) clearTimeout(editPageActivationTimer);
  editPageActivationTimer = setTimeout(() => {
    editPageActivationTimer = null;
    activateCurrentPageForTextEditing(pageIndex);
  }, delay);
}

function historyPointCount() {
  try { return window.AscCommon.History.Points.length; } catch { return -1; }
}

function markTextEditEntry() {
  if (!editModeEntry) editModeEntry = { pointsBefore: historyPointCount(), wasDirty: docDirty };
}

// Active (not user-undone) history points added since entry.
function newActivePoints(entry) {
  try {
    const H = window.AscCommon.History;
    const points = (H && H.Points) || [];
    if (entry.pointsBefore < 0) return null;
    const active = points.slice(0, (typeof H.Index === "number" ? H.Index : points.length - 1) + 1);
    return active.slice(entry.pointsBefore);
  } catch { return null; }
}

// Returns true when there is nothing left to handle (no new points, or the
// pure recognition was rolled back); false when real edits are present.
function rollbackIfPureRecognition(entry) {
  const fresh = newActivePoints(entry);
  if (!fresh || !fresh.length) return true;
  const onlyRecognition = fresh.every(
    (p) => p && p.Description === window.AscDFH.historydescription_Pdf_EditPage);
  if (!onlyRecognition) return false; // real edits — keep them
  try {
    for (let i = 0; i < fresh.length; i++) editor.Undo();
    refreshHistoryButtons();
    if (!entry.wasDirty) markDirty(false);
  } catch (e) {
    console.warn("Erkennungs-Undo fehlgeschlagen:", e);
  }
  return true;
}

// Text edits stay in the drawing model until collectPdfBytes() performs the
// single final raster/OCR conversion required by the standalone sdkjs writer.

// User-selectable marker color (shared by highlight/underline/strikeout),
// persisted across sessions. Highlight applies it translucently, the line
// markers opaquely.
let markerColorHex = "#ffec00";

function markerRgb() {
  return [
    parseInt(markerColorHex.slice(1, 3), 16),
    parseInt(markerColorHex.slice(3, 5), 16),
    parseInt(markerColorHex.slice(5, 7), 16),
  ];
}

async function initMarkerColor() {
  const saved = await storeGet("marker-color");
  if (typeof saved === "string" && /^#[0-9a-fA-F]{6}$/.test(saved)) markerColorHex = saved;
  const input = el("marker-color");
  input.value = markerColorHex;
  input.addEventListener("change", () => {
    markerColorHex = input.value;
    storeSet("marker-color", markerColorHex);
    // Keep both marker backends in sync: editable text is formatted in the
    // drawing model, untouched PDF text uses a regular PDF annotation.
    if (editableMarkerTool) {
      const [r, g, b] = markerRgb();
      editableMarkerTool = {
        ...editableMarkerTool,
        r, g, b,
        opacity: editableMarkerTool.typeName === "Highlight" ? 50 : 100,
      };
    } else if (activeTool.startsWith("marker:")) {
      const typeName = activeTool.slice("marker:".length);
      const [r, g, b] = markerRgb();
      try {
        editor.SetMarkerFormat(annotType(typeName), true,
          typeName === "Highlight" ? 50 : 100, r, g, b);
      } catch { /* keep old color */ }
    }
    refocusEditor();
  });
}

let editableMarkerTool = null;

function getEditableTextSelection(selectWholeObject = false) {
  try {
    const doc = editor.getPDFDoc();
    const active = doc.GetActiveObject();
    if (!active || typeof active.IsDrawing !== "function" || !active.IsDrawing()
        || typeof active.GetSelectionQuads !== "function") return null;

    const controller = doc.GetController();
    const content = controller && typeof controller.getTargetDocContent === "function"
      ? controller.getTargetDocContent(undefined, true)
      : null;
    let quads = active.GetSelectionQuads();
    let selectedWholeObject = false;

    // A single click can select the OCR text object's frame without creating
    // a character selection. For deletion, select that object's text once so
    // the formatting API has a concrete range to clear.
    if ((!quads || !quads.length) && selectWholeObject
        && content && typeof content.SelectAll === "function") {
      content.SelectAll();
      quads = active.GetSelectionQuads();
      selectedWholeObject = true;
    }

    if (!quads || !quads.length) return null;
    return { doc, active, content, quads, selectedWholeObject };
  } catch {
    return null;
  }
}

// Recognized page text is a real ONLYOFFICE drawing text model. Formatting its
// current selection directly keeps runs, caret and undo history intact. The
// normal marker backend remains in use for untouched viewer-layer PDF text.
function applyEditableMarkerSelection() {
  const marker = editableMarkerTool;
  const selection = marker && getEditableTextSelection();
  if (!marker || !selection) return false;

  try {
    if (marker.typeName === "Highlight") {
      // An undefined annotation type intentionally selects CPdfDoc.SetHighlight,
      // whose active-drawing branch applies paragraph/run highlighting.
      selection.doc.SetHighlight(marker.r, marker.g, marker.b, marker.opacity);
    } else if (marker.typeName === "Underline") {
      editor.put_TextPrUnderline(true);
    } else if (marker.typeName === "Strikeout") {
      editor.put_TextPrStrikeout(true);
    } else {
      return false;
    }

    const controller = selection.doc.GetController();
    if (controller && typeof controller.updateSelectionState === "function") {
      controller.updateSelectionState();
    }
    const view = renderer();
    if (view && typeof view.onUpdateOverlay === "function") view.onUpdateOverlay();
    selection.doc.UpdateInterface();
    markCurrentTextPageEdited();
    markDirty(true);
    setStatus("Textformatierung angewendet. Der Text bleibt direkt bearbeitbar.");
    return true;
  } catch (error) {
    console.warn("Textformatierung fehlgeschlagen:", error);
    setStatus(`Textformatierung fehlgeschlagen: ${error.message}`);
    return false;
  }
}

function clearEditableMarkerSelection() {
  const marker = editableMarkerTool;
  const selection = marker && getEditableTextSelection(true);
  if (!marker || !selection) return false;

  try {
    if (marker.typeName === "Highlight") {
      selection.doc.SetHighlight(undefined, undefined, undefined, marker.opacity);
    } else if (marker.typeName === "Underline") {
      editor.put_TextPrUnderline(false);
    } else if (marker.typeName === "Strikeout") {
      editor.put_TextPrStrikeout(false);
    } else {
      return false;
    }

    if (selection.selectedWholeObject && selection.content
        && typeof selection.content.RemoveSelection === "function") {
      selection.content.RemoveSelection();
    }

    const controller = selection.doc.GetController();
    if (controller && typeof controller.updateSelectionState === "function") {
      controller.updateSelectionState();
    }
    const view = renderer();
    if (view && typeof view.onUpdateOverlay === "function") view.onUpdateOverlay();
    selection.doc.UpdateInterface();
    markCurrentTextPageEdited();
    markDirty(true);
    setStatus("Markierung entfernt. Der Text bleibt unverändert.");
    return true;
  } catch (error) {
    console.warn("Markierung konnte nicht entfernt werden:", error);
    setStatus(`Markierung entfernen fehlgeschlagen: ${error.message}`);
    return false;
  }
}

function hasRealTextEditsToCommit() {
  return sessionHasRealEdits();
}

function armMarkerTool(typeName, r, g, b, opacity) {
  if (!docOpen || typeof editor.SetMarkerFormat !== "function") return;
  editableMarkerTool = null;
  editor.SetMarkerFormat(undefined, false);
  setViewerTargetType("select");
  editor.SetMarkerFormat(annotType(typeName), true, opacity, r, g, b);
  setActiveTool("marker:" + typeName);
}

function setMarker(typeName, r, g, b, opacity) {
  if (!docOpen || typeof editor.SetMarkerFormat !== "function") return;
  const toolName = "marker:" + typeName;
  const turningOn = activeTool !== toolName;

  if (!turningOn) {
    editableMarkerTool = null;
    editor.SetMarkerFormat(undefined, false);
    leavePageEditFocus();
    setActiveTool("select");
    return;
  }

  if (hasRealTextEditsToCommit()) {
    editor.SetMarkerFormat(undefined, false);
    editableMarkerTool = { typeName, r, g, b, opacity };
    setViewerTargetType("select");
    // A toolbar mousedown does not steal focus, so a selection made immediately
    // before clicking the marker is still available here.
    const applied = applyEditableMarkerSelection();
    setActiveTool(toolName);
    if (!applied) {
      setStatus("Markierungswerkzeug aktiv — Text im bearbeitbaren Textobjekt auswählen.");
    }
    return;
  }

  leavePageEditFocus();
  armMarkerTool(typeName, r, g, b, opacity);
}

function clearActiveMarkerTools() {
  if (activeTool.startsWith("marker:")) setActiveTool("select");
}

const TOOL_HANDLERS = {
  "undo":        () => { editor.Undo(); refreshHistoryButtons(); },
  "redo":        () => { editor.Redo(); refreshHistoryButtons(); },

  "select":      () => {
    setFormFillMode(false);
    editableMarkerTool = null;
    editor.SetMarkerFormat(undefined, false);
    try { editor.asc_StopInkDrawer(); } catch { /* not drawing */ }
    try { if (editor.isStartAddShape) editor.StartAddShape("rect", false); } catch { /* not armed */ }
    leavePageEditFocus();
    setViewerTargetType("select");
    setActiveTool("select");
  },
  "hand":        () => {
    setFormFillMode(false);
    editableMarkerTool = null;
    editor.SetMarkerFormat(undefined, false);
    try { editor.asc_StopInkDrawer(); } catch { /* not drawing */ }
    try { if (editor.isStartAddShape) editor.StartAddShape("rect", false); } catch { /* not armed */ }
    leavePageEditFocus();
    setViewerTargetType("hand");
    setActiveTool("hand");
    setStatus("Hand-Werkzeug: Seite mit gedrückter Maustaste verschieben.");
  },
  "form-fill":   () => setFormFillMode(activeTool !== "form-fill"),
  "edit-text":   () => {
    // Text editing remains active across page changes. Each reached page is
    // recognized lazily so large documents stay responsive.
    editableMarkerTool = null;
    editor.SetMarkerFormat(undefined, false);
    setViewerTargetType("select");
    setActiveTool("edit-text");
    scheduleCurrentPageTextEditing(editor.getCurrentPage() | 0, 0);
  },
  "textbox":     () => {
    if (typeof editor.AddFreeTextAnnot === "function") {
      const doc = editor.getPDFDoc();
      const previousAnnotations = new Set(doc.annots || []);
      editor.AddFreeTextAnnot(annotType("FreeText") || 2);
      applyDefaultsToNewTextbox(previousAnnotations);
    }
    setActiveTool("textbox");
  },
  "highlight":   () => setMarker("Highlight", ...markerRgb(), 50),
  "underline":   () => setMarker("Underline", ...markerRgb(), 100),
  "strikeout":   () => setMarker("Strikeout", ...markerRgb(), 100),
  "shape":         () => startShape("shape", "rect"),
  "shape-ellipse": () => startShape("shape-ellipse", "ellipse"),
  "shape-line":    () => startShape("shape-line", "line"),
  "shape-arrow":   () => startShape("shape-arrow", "lineWithArrow"),
  "ink":         () => toggleInkPen(),
  "comment":     () => addComment(),
  "image":       () => insertImage(),
  "signature":   (e) => { (e && e.shiftKey) ? openSignaturePad() : insertSignature(); },

  "page-add":    () => { if (typeof editor.asc_AddPage === "function") editor.asc_AddPage((editor.getCurrentPage() | 0) + 1); },
  "page-move":   () => movePageDialog(),
  "page-remove": () => removeCurrentPage(),
  "page-remove-range": () => removePagesByRange(),
  "pdf-append":  () => appendPdf(),
  "pdf-extract": () => extractPages(),
  "rotate-left":  () => rotateCurrentPage(-90),
  "rotate-right": () => rotateCurrentPage(90),
  "rotate-all":   () => rotateAllPages(90),

  "watermark":     () => addWatermark(),
  "page-numbers":  () => addPageNumbers(),
  "extract-images": () => extractEmbeddedImages(),
  "pages-to-images": () => exportPagesAsImages(),
  "ocr":            () => makeSearchablePdf(),
  "ocr-txt":        () => exportRecognizedText(),

  "zoom-out":    () => stepZoom(-1),
  "zoom-in":     () => stepZoom(1),
  "fit-width":   () => { const r = renderer(); r && r.setZoomMode(ZOOM_MODE.Width); },
  "fit-page":    () => { const r = renderer(); r && r.setZoomMode(ZOOM_MODE.Page); },
};

// ── Mouse-interaction guard ───────────────────────────────────────────────
// Once a page has been through the Text tool (asc_EditPage), its content
// lives as editable drawing objects, and CPDFDoc.OnMouseDown hit-tests those
// in EVERY mode: the hand tool then moved text blocks instead of panning,
// and drag-to-select/markers were shadowed because the drawing captured the
// drag. OnMouseDown is also the dispatcher that STARTS text selection and
// feeds the pan state, so it must keep running — instead, blind the object
// HIT-TESTS per tool:
//   • hand: pure pan — drawings, annotations and fields are all invisible
//     to the mouse (links keep working).
//   • select/marker: page-content drawings are invisible (text selection
//     wins); annotations/fields (textboxes, signatures, images as annots,
//     form fields) stay interactive.
//   • Text tool (edit-text) and everything else: untouched — page content
//     is edited there.
function installDocMouseGuard() {
  const viewer = editor.getDocumentRenderer && editor.getDocumentRenderer();
  if (!viewer || viewer.__hitTestGuardInstalled) return;
  viewer.__hitTestGuardInstalled = true;

  const guards = {
    getPageDrawingByMouse: () =>
      activeTool === "hand" || activeTool === "select"
        || (activeTool.startsWith("marker:") && !editableMarkerTool),
    getPageAnnotByMouse: () => activeTool === "hand",
    getPageFieldByMouse: () => activeTool === "hand",
  };
  for (const [method, isBlinded] of Object.entries(guards)) {
    if (typeof viewer[method] !== "function") continue;
    const orig = viewer[method].bind(viewer);
    viewer[method] = function (...args) {
      if (mode === "editor" && docOpen && isBlinded()) return null;
      return orig(...args);
    };
  }
}

// Shape drawing. StartAddShape takes an OOXML preset name; the engine turns
// the drawn geometry into a PDF drawing. Clicking the active shape tool again
// leaves shape mode.
// NOTE the is_apply flag: true ARMS add-shape mode (locks the crosshair
// cursor, next drag draws the preset); false ENDS it (sync_EndAddShape +
// sync_StartAddShapeCallback(false)). Getting this backwards makes the shape
// tools silently do nothing.
function startShape(toolName, preset) {
  if (typeof editor.StartAddShape !== "function") return;
  if (activeTool === toolName) {
    try { editor.StartAddShape(preset, false); } catch { /* leave draw mode */ }
    setActiveTool("select");
    return;
  }
  editor.StartAddShape(preset, true);
  setActiveTool(toolName);
}

// Freehand pen: the engine's ink drawer turns drawn strokes into ink
// annotations. Width is in EMU (12700 per pt).
function toggleInkPen() {
  if (activeTool === "ink") {
    try { editor.asc_StopInkDrawer(); } catch { /* not drawing */ }
    setActiveTool("select");
    setStatus("Stift beendet.");
    return;
  }
  try {
    const pen = new window.AscFormat.CLn();
    pen.w = 2 * 12700; // 2 pt
    pen.Fill = window.AscFormat.CreateSolidFillRGB(16, 26, 134); // ink blue
    editor.asc_StartDrawInk(pen);
    setActiveTool("ink");
    setStatus(`Stift aktiv: mit gedrückter Maustaste zeichnen. „Auswahl" beendet den Modus.`);
  } catch (e) {
    console.error("Stift konnte nicht gestartet werden:", e);
    setStatus("Stift konnte nicht gestartet werden.");
  }
}

// Move the current page to a new position (1-based prompt). The thumbnail
// rail also supports drag & drop; this is the keyboard/menu route. MovePages
// is the engine's own undoable page-reorder primitive.
async function movePageDialog() {
  if (!docOpen || mode !== "editor") return;
  const pageCount = editor.getCountPages() | 0;
  if (pageCount < 2) { setStatus("Nur eine Seite — nichts zu verschieben."); return; }
  const cur = (editor.getCurrentPage() | 0) + 1;
  const spec = await showPromptDialog(
    `Aktuelle Seite (${cur}) verschieben an Position (1–${pageCount}):`, String(cur));
  if (!spec) return;
  const target = parseInt(spec, 10);
  if (!Number.isInteger(target) || target < 1 || target > pageCount) {
    setStatus(`Ungültige Zielposition: „${spec}" (erlaubt: 1–${pageCount}).`);
    return;
  }
  if (target === cur) return;
  try {
    const doc = editor.getPDFDoc();
    doc.DoAction(function () {
      doc.MovePages([cur - 1], target - 1);
      doc.Viewer.navigateToPage(target - 1);
    }, window.AscDFH.historydescription_Pdf_MovePage, doc, [Math.min(cur, target) - 1]);
    refreshHistoryButtons();
    setStatus(`Seite ${cur} an Position ${target} verschoben.`);
  } catch (e) {
    console.error("Seite verschieben fehlgeschlagen:", e);
    setStatus(`Seite verschieben fehlgeschlagen: ${e.message}`);
  }
}

// Insert an image from a data URL through the editor's image pipeline
// (shared by the "Bild" tool and the signature stamp).
function insertImageDataUrl(dataUrl) {
  if (!docOpen) return;
  try {
    if (typeof editor.AddImageUrl === "function") editor.AddImageUrl([dataUrl]);
  } catch (e) {
    console.error("Bild einfügen fehlgeschlagen:", e);
    setStatus("Bild konnte nicht eingefügt werden.");
  }
}

async function addComment() {
  if (!docOpen) return;
  const text = await showPromptDialog("Kommentartext:");
  if (!text) return;
  try {
    const data = new window.Asc.asc_CCommentData();
    data.asc_putText(text);
    if (editor.User && editor.User.asc_getUserName) data.asc_putUserName(editor.User.asc_getUserName());
    editor.asc_addComment(data);
    setStatus("Kommentar hinzugefügt.");
  } catch (e) {
    console.error(e);
    setStatus("Kommentar konnte nicht hinzugefügt werden (Markierung wählen).");
  }
}

function insertImage() {
  if (!docOpen) return;
  // Pick a local image and insert it via the editor's image pipeline.
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/*";
  input.onchange = () => {
    const f = input.files && input.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      insertImageDataUrl(reader.result);
      setStatus("Bild eingefügt.");
    };
    reader.readAsDataURL(f);
  };
  input.click();
}

// Append all pages of another PDF to the end of the open document. Uses the
// engine's real PDF merge (CPDFDoc.MergePagesBinary → WASM MergePages), the
// same machinery its cross-document page paste uses: pages arrive with their
// original content, fonts and annotations, and the operation is undoable.
async function appendPdfFile(file) {
  if (!file || !docOpen || mode !== "editor") return false;
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    if (String.fromCharCode(...bytes.slice(0, 5)) !== "%PDF-") {
      setStatus(`„${file.name}" ist keine gültige PDF-Datei.`);
      return false;
    }

    setStatus(`„${file.name}" wird angehängt …`);
    const doc = editor.getPDFDoc();
    const insertPos = editor.getCountPages() | 0;
    doc.DoAction(function () {
      doc.MergePagesBinary(insertPos, bytes);
    }, window.AscDFH.historydescription_Pdf_AddPage, doc);
    suppressFormDesignLabels();
    refreshHistoryButtons();
    markDirty(true);
    setStatus(`„${file.name}" angehängt — ${editor.getCountPages()} Seiten insgesamt.`);
    return true;
  } catch (e) {
    console.error("PDF anhängen fehlgeschlagen:", e);
    setStatus(`PDF anhängen fehlgeschlagen: ${e.message}`);
    return false;
  }
}

function appendPdf() {
  if (!docOpen || mode !== "editor") return;
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "application/pdf";
  input.onchange = () => appendPdfFile(input.files && input.files[0]);
  input.click();
}

// Parse a Stirling-PDF/PDFSam-style page range spec ("1-3,5,7-9") into a
// sorted, de-duplicated array of 0-based page indexes, validated against the
// document's actual page count.
function parsePageRangeSpec(spec, pageCount) {
  const indexes = new Set();
  for (const part of spec.split(",").map((s) => s.trim()).filter(Boolean)) {
    const m = part.match(/^(\d+)(?:-(\d+))?$/);
    if (!m) throw new Error(`Ungültiger Bereich: „${part}"`);
    const start = parseInt(m[1], 10);
    const end = m[2] !== undefined ? parseInt(m[2], 10) : start;
    if (start < 1 || end < start || end > pageCount) {
      throw new Error(`Seite außerhalb des gültigen Bereichs (1–${pageCount}): „${part}"`);
    }
    for (let p = start; p <= end; p++) indexes.add(p - 1);
  }
  if (!indexes.size) throw new Error("Kein gültiger Seitenbereich angegeben.");
  return Array.from(indexes).sort((a, b) => a - b);
}

// Extract a page range into a standalone PDF file (Stirling-PDF/PDFSam
// "split"/"extract pages" equivalent). Uses CPDFDoc.GetPagesBinary, the same
// WASM serializer collectPdfBytes() uses for "Speichern" — but restricted to
// the chosen page indexes. This reads the document, it never mutates it, so
// there's nothing to undo afterwards.
async function extractPages() {
  if (!docOpen || mode !== "editor") return;
  const pageCount = editor.getCountPages() | 0;
  const spec = await showPromptDialog(
    `Welche Seiten sollen als neue PDF-Datei exportiert werden?\nz.B. "1-3,5" — Dokument hat ${pageCount} Seite(n).`,
    `1-${pageCount}`
  );
  if (!spec) return;

  let indexes;
  try {
    indexes = parsePageRangeSpec(spec, pageCount);
  } catch (e) {
    setStatus(`Extrahieren fehlgeschlagen: ${e.message}`);
    return;
  }

  setStatus("PDF wird erzeugt …");
  try {
    const doc = editor.getPDFDoc();
    try { doc.BlurActiveObject(); } catch { /* commits an active form field */ }
    const result = doc.GetPagesBinary(indexes, false);
    if (!result || !result.length || String.fromCharCode(...result.slice(0, 5)) !== "%PDF-") {
      setStatus("Extrahieren fehlgeschlagen: keine gültigen PDF-Daten von der Engine.");
      return;
    }
    const bytes = await bakeShapesIntoPdf(
      result instanceof Uint8Array ? result : new Uint8Array(result), indexes);
    const suffix = indexes.length === pageCount ? "alle-Seiten" : `Seiten-${spec.replace(/[^0-9,-]/g, "")}`;
    const outName = lastName.replace(/\.pdf$/i, "") + `-${suffix}.pdf`;

    if (window.desktop && typeof window.desktop.savePdf === "function") {
      const res = await window.desktop.savePdf(bytes, outName);
      setStatus(res && res.saved
        ? `Gespeichert: ${res.path}`
        : (res && res.error ? `Speichern fehlgeschlagen: ${res.error}` : "Speichern abgebrochen."));
      return;
    }
    downloadBytes(bytes, outName);
    setStatus(`„${outName}" erzeugt — ${indexes.length} Seite(n).`);
  } catch (e) {
    console.error("Seiten extrahieren fehlgeschlagen:", e);
    setStatus(`Extrahieren fehlgeschlagen: ${e.message}`);
  }
}

// Register every form field's font in the document's loadedFonts list right
// after open. CPDFDoc.getTextController() gates text entry on
// checkFieldFont(field): the FIRST keystroke into a field otherwise returns
// false (and is dropped) because that very call is what populates the list —
// even though the font files themselves are long since in memory.
function preloadFieldFonts() {
  try {
    const doc = editor.getPDFDoc && editor.getPDFDoc();
    if (!doc || !Array.isArray(doc.widgets)) return;
    for (const field of doc.widgets) {
      try { doc.checkFieldFont(field, function () { /* fonts registered */ }); } catch { /* per-field best effort */ }
    }
  } catch (e) {
    console.warn("Formular-Fonts vorladen fehlgeschlagen:", e);
  }
}

// ONLYOFFICE equates general PDF editing with form-design mode. In that mode
// every AcroForm widget gets a temporary edit shape containing its internal
// field name ("Text1", "DatumRow1", ...). This app supports filling existing
// forms, not redesigning their widgets, so keep the real field metadata but
// use the normal fill presentation while all other PDF editing stays enabled.
function suppressFormDesignLabels() {
  try {
    const doc = editor.getPDFDoc && editor.getPDFDoc();
    if (!doc || !Array.isArray(doc.widgets)) return;

    if (!doc.__formDesignLabelsSuppressed) {
      doc.IsEditFieldsMode = function () { return false; };
      doc.__formDesignLabelsSuppressed = true;
    }

    for (const field of doc.widgets) {
      try {
        if (typeof field.SetEditMode === "function") field.SetEditMode(false);
        if (typeof field.SetNeedUpdateEditShape === "function") {
          field.SetNeedUpdateEditShape(false);
        }
      } catch { /* per-field best effort */ }
    }
  } catch (e) {
    console.warn("Formular-Entwurfsbeschriftungen konnten nicht ausgeblendet werden:", e);
  }
}

// ── Form fill mode ────────────────────────────────────────────────────────
// The engine treats form widgets as DESIGN objects while canEdit() is true
// (IsEditFieldsMode: clicking a field selects/moves it). Filling requires the
// OnlyForms restriction: content editing is locked, fields become fillable —
// exactly how the upstream editor separates "edit" from "fill" mode.
function setFormFillMode(on) {
  if (mode !== "editor" || !docOpen) return;
  const R = window.Asc.c_oAscRestrictionType;
  try {
    editor.asc_setRestriction(on ? R.OnlyForms : R.None);
  } catch (e) {
    console.warn("Formularmodus umschalten fehlgeschlagen:", e);
    return;
  }
  // tools that edit content are unavailable while filling
  const editTools = ["edit-text", "textbox", "highlight", "underline", "strikeout",
    "shape", "shape-ellipse", "shape-line", "shape-arrow", "ink", "comment", "image",
    "signature", "page-add", "page-remove", "page-remove-range", "page-move", "pdf-append",
    "rotate-left", "rotate-right", "rotate-all", "watermark", "page-numbers"];
  for (const tool of editTools) setToolEnabled(tool, !on);
  setFormatEnabled(!on);
  setActiveTool(on ? "form-fill" : "select");
  setStatus(on
    ? "Formularmodus: Felder anklicken und ausfüllen. „Auswahl“ beendet den Modus."
    : "Bearbeitungsmodus.");
}

async function discardCurrentDocument() {
  const dirtyHint = docDirty ? " Alle ungespeicherten Änderungen gehen dabei verloren." : "";
  if (!window.confirm(`Die letzte Seite löschen und das aktuelle Dokument schließen?${dirtyHint}`)) return false;

  markDirty(false);
  await clearRecoverySnapshot();
  try { sessionStorage.removeItem(TEXT_COMMIT_TRANSITION_KEY); } catch { /* no session storage */ }
  setStatus("Dokument wird geschlossen …");
  location.reload();
  return true;
}

async function removeCurrentPage() {
  if (!docOpen || typeof editor.asc_RemovePage !== "function") return;
  const cur = editor.getCurrentPage() | 0;
  if (typeof editor.getCountPages === "function" && editor.getCountPages() <= 1) {
    await discardCurrentDocument();
    return;
  }
  editor.asc_RemovePage([cur]);
  refreshHistoryButtons();
}

// Stirling-PDF/PDFSam-style "remove pages by range" ("2-4,7"), reusing the
// same range spec parser as "Teilen"/extract. asc_RemovePage already accepts
// a list of page indexes and is undoable (Strg+Z restores them all).
async function removePagesByRange() {
  if (!docOpen || typeof editor.asc_RemovePage !== "function") return;
  const pageCount = editor.getCountPages() | 0;
  const spec = await showPromptDialog(
    `Welche Seiten sollen gelöscht werden?\nz.B. "2-4,7" — Dokument hat ${pageCount} Seite(n).`
  );
  if (!spec) return;

  let indexes;
  try {
    indexes = parsePageRangeSpec(spec, pageCount);
  } catch (e) {
    setStatus(`Entfernen fehlgeschlagen: ${e.message}`);
    return;
  }
  if (indexes.length >= pageCount) {
    await discardCurrentDocument();
    return;
  }

  editor.asc_RemovePage(indexes);
  refreshHistoryButtons();
  setStatus(`${indexes.length} Seite(n) entfernt — ${editor.getCountPages()} verbleiben.`);
}

function rotateCurrentPage(angle) {
  if (!docOpen || typeof editor.asc_RotatePage !== "function") return;
  editor.asc_RotatePage(angle, [editor.getCurrentPage() | 0]);
  refreshHistoryButtons();
}

// Batch page rotation (Stirling-PDF's "Rotate PDF" equivalent): rotate every
// page in the document at once, e.g. for scans that all came in sideways.
function rotateAllPages(angle) {
  if (!docOpen || typeof editor.asc_RotatePage !== "function") return;
  const pageCount = editor.getCountPages() | 0;
  if (!pageCount) return;
  const allIndexes = Array.from({ length: pageCount }, (_, i) => i);
  editor.asc_RotatePage(angle, allIndexes);
  refreshHistoryButtons();
  setStatus(`Alle ${pageCount} Seiten um ${angle}° gedreht.`);
}

// Runs fn(doc, nPage, pageW, pageH, rotAngle) once per page inside a single
// DoAction transaction, so the whole batch is one undo step.
function forEachPageInTransaction(fn) {
  const doc = editor.getPDFDoc();
  const pageCount = doc.GetPagesCount();
  doc.DoAction(function () {
    for (let nPage = 0; nPage < pageCount; nPage++) {
      const rotAngle = doc.Viewer.getPageRotate(nPage);
      fn(doc, nPage, doc.GetPageWidth(nPage), doc.GetPageHeight(nPage), rotAngle);
    }
  }, window.AscDFH.historydescription_Pdf_AddAnnot, doc);
}

// Adds a FreeText annotation on page nPage at a fixed position (not tied to
// the current viewport/mouse, unlike the interactive AddFreeTextAnnot the
// "Textfeld" tool uses). anchor "center" is rotation-invariant (a page's
// center point doesn't move when the page rotates around it); "bottom-center"
// anchors in unrotated page space, which is only exactly correct for
// rotAngle === 0 — an accepted simplification for page numbers on the common
// case, not a crash risk on rotated pages (worst case: wrong edge visually).
function addPageFreeText(doc, nPage, pageW, pageH, rotAngle, text, opts) {
  const extX = opts.width, extY = opts.height;
  const cx = pageW / 2;
  // rect's Y axis grows downward (screen-space), not PDF's bottom-up native
  // space, so "bottom" of the page is the LARGE Y end, near pageH.
  const cy = opts.anchor === "bottom-center" ? pageH - (opts.margin || 24) : pageH / 2;
  const X1 = cx - extX / 2, Y1 = cy - extY / 2, X2 = X1 + extX, Y2 = Y1 + extY;
  const now = Date.now();
  const oAnnot = doc.AddAnnotByProps({
    rect: [X1, Y1, X2, Y2],
    page: nPage,
    name: window.AscCommon.CreateGUID(),
    type: annotType("FreeText"),
    author: (editor.User && editor.User.asc_getUserName()) || "PDF Editor",
    modDate: now, creationDate: now, contents: text, hidden: false,
  });
  oAnnot.SetRotate(rotAngle);
  // undefined → CreateNoFillUniFill (fully transparent, no box behind the
  // text). NOT [] — an empty array falls through GetRGBColor's default and
  // yields solid white.
  oAnnot.SetFillColor(undefined);
  oAnnot.SetBorderWidth(0);
  oAnnot.SetOpacity(opts.opacity != null ? opts.opacity : 1);
  oAnnot.SetAlign(window.AscPDF.ALIGN_TYPE.center);
  oAnnot.SetRichContents([{
    text, size: opts.fontSize || 24, color: opts.color || [0, 0, 0],
    bold: false, italic: false, underlined: false, strikethrough: false,
    alignment: window.AscPDF.ALIGN_TYPE.center,
  }]);
  return oAnnot;
}

// Lazy-load the vendored pdf-lib bundle (MIT; public/js/vendor/pdf-lib.min.js).
let pdfLibPromise = null;
function loadPdfLib() {
  if (!pdfLibPromise) {
    pdfLibPromise = window.PDFLib ? Promise.resolve() : loadScript("/js/vendor/pdf-lib.min.js");
  }
  return pdfLibPromise;
}

// Word-style text watermark: diagonal across the page centre, light grey,
// translucent, on every page — BAKED into the page content with pdf-lib.
//
// Why not an engine object? A FreeText annotation can't rotate freely (its
// SetRotate only implements 0/90/180/270 as text-direction flips), and
// drawings (CPdfShape, which CAN rotate arbitrarily) don't survive saving at
// all: the engine's split-based save stream (SaveForSplit → WASM SplitPages)
// has no drawing serialization — the WASM writer traps on shape commands.
// Baking the watermark into the PDF bytes (what Stirling-PDF does too) works
// in every viewer and can't get lost. Trade-off: it's applied permanently
// (the document reloads; no Strg+Z), which the prompt text makes clear.
async function addWatermark() {
  if (!docOpen || mode !== "editor") return;
  const text = await showPromptDialog(
    "Wasserzeichen-Text (wird dauerhaft auf allen Seiten eingebettet):");
  if (!text) return;
  setStatus("Wasserzeichen wird eingefügt …");
  try {
    await loadPdfLib();
    const bytes = await collectPdfBytes(); // carries all edits made so far
    if (!bytes) {
      setStatus("Wasserzeichen fehlgeschlagen: keine gültigen PDF-Daten von der Engine.");
      return;
    }
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
  initTextboxBorderControls();
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
    setTimeout(syncTextboxBorderControls, 0);
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
