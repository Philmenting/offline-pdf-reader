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

const ZOOM_STEPS = [50, 75, 90, 100, 110, 125, 150, 175, 200, 250, 300, 400];
const ZOOM_MODE  = { Custom: 0, Width: 1, Page: 2 };

const el = (id) => document.getElementById(id);
const statusEl = el("status");
const setStatus = (msg) => { statusEl.textContent = msg; };

let editor = null;       // Asc.PDFEditorApi instance (editor mode)
let viewer = null;       // AscViewer.CViewer instance (fallback mode)
let thumbnails = null;
let mode = "loading";    // "editor" | "viewer" | "loading"
let docOpen = false;
let lastName = "document.pdf";
let activeTool = "select";
let editorErrorMsg = null; // why we fell back to read-only mode (if we did)

if (typeof window["g_fonts_selection_bin"] === "undefined") {
  window["g_fonts_selection_bin"] = "";
}

// ── Font substitution patch ──────────────────────────────────────────────
// The engine's font manager scores fonts via g_fonts_selection_bin. We ship no
// precomputed binary, so we patch pickFont to map common PDF font names onto our
// bundled Liberation/DejaVu families. Applies to both viewer and editor builds
// (same AscFonts global).
function installFontSubstitutionPatch() {
  const af = window.AscFonts;
  if (!af || !af.jh || !af.kh || af.__substPatched) return false;

  const SUBS = {
    "Arial": "Liberation Sans", "Arial Narrow": "Liberation Sans Narrow",
    "Helvetica": "Liberation Sans", "Helvetica Neue": "Liberation Sans",
    "Times New Roman": "Liberation Serif", "Times": "Liberation Serif",
    "Times Roman": "Liberation Serif", "TimesNewRoman": "Liberation Serif",
    "TimesNewRomanPS": "Liberation Serif", "TimesNewRomanPSMT": "Liberation Serif",
    "ArialMT": "Liberation Sans", "CourierNewPSMT": "Liberation Mono",
    "Courier New": "Liberation Mono", "Courier": "Liberation Mono",
    "Calibri": "Carlito", "Cambria": "Caladea", "Verdana": "DejaVu Sans",
    "Georgia": "DejaVu Serif", "Tahoma": "DejaVu Sans", "Trebuchet MS": "DejaVu Sans",
    "Lucida Sans": "DejaVu Sans", "Lucida Console": "DejaVu Sans Mono",
    "Consolas": "DejaVu Sans Mono", "Segoe UI": "DejaVu Sans",
    "Palatino": "DejaVu Serif", "Palatino Linotype": "DejaVu Serif",
    "Book Antiqua": "DejaVu Serif", "Garamond": "DejaVu Serif",
    "Century": "DejaVu Serif", "Impact": "Liberation Sans",
    "Comic Sans MS": "DejaVu Sans", "Symbol": "Symbola", "ZapfDingbats": "Symbola",
  };

  const STYLE_KEYWORDS = {
    "Bold": 1, "Bd": 1, "Demi": 1, "Heavy": 1, "Black": 1,
    "Italic": 2, "It": 2, "Oblique": 2, "Obl": 2, "Slanted": 2,
    "BoldItalic": 3, "BoldOblique": 3, "BoldIt": 3,
    "Roman": 0, "Regular": 0, "Book": 0, "Medium": 0, "Light": 0,
  };

  function parsePostScriptName(psName) {
    const dashIdx = psName.indexOf("-");
    if (dashIdx < 0) {
      const commaIdx = psName.indexOf(",");
      if (commaIdx < 0) return { family: psName, styleOverride: null };
      const suffix = psName.slice(commaIdx + 1).trim();
      if (STYLE_KEYWORDS[suffix] !== undefined) {
        return { family: psName.slice(0, commaIdx), styleOverride: STYLE_KEYWORDS[suffix] };
      }
      return { family: psName, styleOverride: null };
    }
    const family = psName.slice(0, dashIdx);
    const suffix = psName.slice(dashIdx + 1);
    if (STYLE_KEYWORDS[suffix] !== undefined) {
      return { family, styleOverride: STYLE_KEYWORDS[suffix] };
    }
    return { family: psName, styleOverride: null };
  }

  const origPickFont = af.pickFont;
  af.pickFont = function (name, style) {
    const kh = af.kh, jh = af.jh;
    let resolvedName = name;
    let effectiveStyle = style;

    if (kh[resolvedName] === undefined) {
      const ps = parsePostScriptName(name);
      if (ps.styleOverride !== null) {
        resolvedName = ps.family;
        effectiveStyle = ps.styleOverride;
      }
    }
    if (kh[resolvedName] === undefined && SUBS[resolvedName]) resolvedName = SUBS[resolvedName];
    if (kh[resolvedName] === undefined) {
      const upper = resolvedName.toUpperCase();
      for (const k of Object.keys(kh)) {
        if (k.toUpperCase() === upper) { resolvedName = k; break; }
      }
    }
    if (kh[resolvedName] === undefined && SUBS[name]) resolvedName = SUBS[name];

    if (kh[resolvedName] !== undefined) {
      const entry = jh[kh[resolvedName]];
      if (entry && entry.Mn) return entry.Mn(AscCommon.je, effectiveStyle).file;
    }
    return origPickFont.call(this, name, style);
  };

  af.__substPatched = true;
  return true;
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.async = false;
    s.onload = () => resolve(src);
    s.onerror = () => reject(new Error(`Konnte ${src} nicht laden`));
    document.head.appendChild(s);
  });
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
  setStatus("PDF-Editor wird geladen …");

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
  installFontSubstitutionPatch();
  setStatus("Bereit. Öffne eine PDF-Datei zum Bearbeiten.");
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
    enableEditing(true);
    setStatus(`„${lastName}" geöffnet — bereit zum Bearbeiten.`);
    setupThumbnails();
    refreshHistoryButtons();
  });
  on("asc_onCountPages", (n) => { /* page count available */ });
  on("asc_onCurrentPage", (n) => { /* current page changed */ });
  on("asc_onCanUndo", (v) => setToolEnabled("undo", docOpen && !!v));
  on("asc_onCanRedo", (v) => setToolEnabled("redo", docOpen && !!v));
  on("asc_onMarkerFormatChanged", (type, isOn) => {
    // keep the toolbar's active highlight in sync if the engine toggles it off
    if (!isOn) clearActiveMarkerTools();
  });
}

function setupThumbnails() {
  try {
    const renderer = editor.getDocumentRenderer && editor.getDocumentRenderer();
    if (renderer && typeof renderer.createThumbnails === "function") {
      thumbnails = renderer.createThumbnails("thumbnails");
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

// ── Viewer fallback (read-only) ───────────────────────────────────────────
async function initViewerFallback() {
  setStatus("Editor nicht verfügbar — lade Lese-Engine …");
  await loadScript(VIEWER_BUNDLE);
  if (!(window.AscViewer && typeof window.AscViewer.CViewer === "function")) {
    throw new Error("Weder Editor noch Viewer-Engine verfügbar.");
  }
  el("editor_sdk").hidden = true;
  el("viewer-container").hidden = false;

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
function openArrayBuffer(buf, name) {
  const bytes = new Uint8Array(buf);
  const magic = String.fromCharCode(...bytes.slice(0, 5));
  if (magic !== "%PDF-") {
    setStatus(`„${name}" ist keine gültige PDF-Datei.`);
    return;
  }
  lastName = name;
  el("placeholder").style.display = "none";

  try {
    if (mode === "editor") {
      installFontSubstitutionPatch();
      editor.openDocument({ data: bytes });   // browser open: no server, no upload
      setStatus(`„${name}" wird geöffnet …`);
    } else if (mode === "viewer") {
      viewer.open(buf);
      installFontSubstitutionPatch();
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

function onFileChosen(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => openArrayBuffer(reader.result, file.name);
  reader.onerror = () => setStatus("Datei konnte nicht gelesen werden.");
  reader.readAsArrayBuffer(file);
}

function saveDocument() {
  if (mode !== "editor" || !docOpen) return;
  setStatus("PDF wird erzeugt …");
  try {
    const renderer = editor.getDocumentRenderer();
    const result = renderer && typeof renderer.Save === "function" ? renderer.Save() : null;
    if (!result || !result.length) {
      setStatus("Speichern fehlgeschlagen: keine Daten von der Engine.");
      return;
    }
    const bytes = result instanceof Uint8Array ? result : new Uint8Array(result);
    const outName = lastName.replace(/\.pdf$/i, "") + "-bearbeitet.pdf";
    downloadBytes(bytes, outName);
    setStatus(`Gespeichert: „${outName}".`);
  } catch (e) {
    console.error("save() error:", e);
    setStatus(`Speichern fehlgeschlagen: ${e.message}`);
  }
}

function downloadBytes(bytes, name) {
  const blob = new Blob([bytes], { type: "application/pdf" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

// ── Editing tools ─────────────────────────────────────────────────────────
function renderer() {
  if (mode === "editor" && editor && editor.getDocumentRenderer) return editor.getDocumentRenderer();
  return viewer; // fallback CViewer exposes the same setZoom/getZoom/setZoomMode
}
function annotType(name) { return (window.AscPDF && window.AscPDF.ANNOTATIONS_TYPES) ? window.AscPDF.ANNOTATIONS_TYPES[name] : undefined; }

function setMarker(typeName, r, g, b, opacity) {
  if (!docOpen || typeof editor.SetMarkerFormat !== "function") return;
  const type = annotType(typeName);
  const turningOn = activeTool !== ("marker:" + typeName);
  // turn any current marker off first
  editor.SetMarkerFormat(undefined, false);
  if (turningOn) {
    editor.SetMarkerFormat(type, true, opacity, r, g, b);
    setActiveTool("marker:" + typeName);
  } else {
    setActiveTool("select");
  }
}

function clearActiveMarkerTools() {
  if (activeTool.startsWith("marker:")) setActiveTool("select");
}

const TOOL_HANDLERS = {
  "undo":        () => { editor.Undo(); refreshHistoryButtons(); },
  "redo":        () => { editor.Redo(); refreshHistoryButtons(); },

  "select":      () => { editor.SetMarkerFormat(undefined, false); setActiveTool("select"); },
  "edit-text":   () => { if (typeof editor.asc_EditPage === "function") editor.asc_EditPage(); setActiveTool("edit-text"); },
  "textbox":     () => { if (typeof editor.AddFreeTextAnnot === "function") editor.AddFreeTextAnnot(annotType("FreeText") || 2); setActiveTool("textbox"); },
  "highlight":   () => setMarker("Highlight", 255, 236, 0, 1),
  "underline":   () => setMarker("Underline", 220, 30, 30, 1),
  "strikeout":   () => setMarker("Strikeout", 220, 30, 30, 1),
  "shape":       () => { if (typeof editor.StartAddShape === "function") editor.StartAddShape("rect", false); setActiveTool("shape"); },
  "comment":     () => addComment(),
  "image":       () => insertImage(),

  "page-add":    () => { if (typeof editor.asc_AddPage === "function") editor.asc_AddPage((editor.getCurrentPage() | 0) + 1); },
  "page-remove": () => removeCurrentPage(),
  "rotate-left":  () => rotateCurrentPage(-90),
  "rotate-right": () => rotateCurrentPage(90),

  "zoom-out":    () => stepZoom(-1),
  "zoom-in":     () => stepZoom(1),
  "fit-width":   () => { const r = renderer(); r && r.setZoomMode(ZOOM_MODE.Width); },
  "fit-page":    () => { const r = renderer(); r && r.setZoomMode(ZOOM_MODE.Page); },
};

function addComment() {
  if (!docOpen) return;
  const text = window.prompt("Kommentartext:");
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
      try {
        const dataUrl = reader.result;
        if (typeof editor.AddImageUrl === "function") editor.AddImageUrl([dataUrl]);
        else if (typeof editor.asc_addImage === "function") editor.asc_addImage();
        setStatus("Bild eingefügt.");
      } catch (e) {
        console.error(e);
        setStatus("Bild konnte nicht eingefügt werden.");
      }
    };
    reader.readAsDataURL(f);
  };
  input.click();
}

function removeCurrentPage() {
  if (!docOpen || typeof editor.asc_RemovePage !== "function") return;
  const cur = editor.getCurrentPage() | 0;
  if (typeof editor.getCountPages === "function" && editor.getCountPages() <= 1) {
    setStatus("Die letzte Seite kann nicht gelöscht werden.");
    return;
  }
  editor.asc_RemovePage([cur]);
  refreshHistoryButtons();
}

function rotateCurrentPage(angle) {
  if (!docOpen || typeof editor.asc_RotatePage !== "function") return;
  editor.asc_RotatePage(angle, [editor.getCurrentPage() | 0]);
  refreshHistoryButtons();
}

function stepZoom(dir) {
  const r = renderer();
  if (!r) return;
  const z = Math.round(r.getZoom ? r.getZoom() : 100);
  const next = dir > 0 ? ZOOM_STEPS.find((v) => v > z) : [...ZOOM_STEPS].reverse().find((v) => v < z);
  if (next) r.setZoom(next);
  setStatus(`Zoom: ${Math.round(r.getZoom ? r.getZoom() : z)} %`);
}

// ── Toolbar state ─────────────────────────────────────────────────────────
function setActiveTool(name) {
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
  "undo", "redo", "select", "edit-text", "textbox", "highlight", "underline",
  "strikeout", "shape", "comment", "image", "page-add", "page-remove",
  "rotate-left", "rotate-right", "zoom-out", "zoom-in", "fit-width", "fit-page",
];

function enableEditing(on) {
  el("btn-save").disabled = !(on && mode === "editor");
  for (const tool of EDITOR_TOOLS) {
    // in viewer fallback, only view tools are usable
    const viewerOk = ["zoom-out", "zoom-in", "fit-width", "fit-page"].includes(tool);
    setToolEnabled(tool, on && (mode === "editor" || viewerOk));
  }
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
  el("btn-save").addEventListener("click", saveDocument);

  for (const btn of document.querySelectorAll("[data-tool]")) {
    const tool = btn.getAttribute("data-tool");
    btn.addEventListener("click", () => {
      const handler = TOOL_HANDLERS[tool];
      if (!handler) return;
      try { handler(); } catch (e) {
        console.error(`Tool '${tool}' fehlgeschlagen:`, e);
        setStatus(`Aktion „${tool}" fehlgeschlagen: ${e.message}`);
      }
    });
  }

  const host = document.querySelector(".viewer-host");
  host.addEventListener("dragover", (e) => e.preventDefault());
  host.addEventListener("drop", (e) => {
    e.preventDefault();
    onFileChosen(e.dataTransfer.files && e.dataTransfer.files[0]);
  });

  window.addEventListener("resize", () => {
    if (mode === "editor" && editor && editor.WordControl) {
      try { editor.WordControl.OnResize(true); } catch { /* ignore */ }
    }
  });
}

wireUi();
initEditor().catch((err) => {
  editorErrorMsg = (err && err.message) ? err.message : String(err);
  console.error("Editor-Bootstrap fehlgeschlagen:", err);
  setStatus(`Editor konnte nicht geladen werden: ${editorErrorMsg} — Wechsel in den Nur-Lese-Modus …`);
  initViewerFallback().catch((err2) => {
    console.error(err2);
    setStatus(`Fehler beim Laden der Engine: ${err2.message}`);
  });
});
