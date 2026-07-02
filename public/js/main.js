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
// precomputed binary, so pickFont() mis-resolves common PDF fonts (Arial,
// Times, …) → garbled text. We patch pickFont to map those names onto our
// bundled Liberation/DejaVu families.
//
// Two builds expose different internals:
//   • Viewer (closure-minified engine/viewer.js): AscFonts.jh/.kh name index.
//   • Editor (concatenated common/libfont/map.js): pickFont() delegates to
//     g_fontApplication.GetFontInfo(name, style) — no jh/kh.
// We support both: rewrite the requested name to a bundled family, then let the
// build's own resolution take over.
const FONT_SUBS = {
  "Arial": "Liberation Sans", "Arial Narrow": "Liberation Sans Narrow",
  "Helvetica": "Liberation Sans", "Helvetica Neue": "Liberation Sans",
  "Times New Roman": "Liberation Serif", "Times": "Liberation Serif",
  "Times Roman": "Liberation Serif", "TimesNewRoman": "Liberation Serif",
  "TimesNewRomanPS": "Liberation Serif", "TimesNewRomanPSMT": "Liberation Serif",
  "ArialMT": "Liberation Sans", "Arial-BoldMT": "Liberation Sans",
  "Arial-ItalicMT": "Liberation Sans", "Arial-BoldItalicMT": "Liberation Sans",
  "CourierNewPSMT": "Liberation Mono", "CourierNewPS": "Liberation Mono",
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

// "Arial-BoldMT" → {family:"Arial", styleOverride:1}; "ArialMT" → {family:"Arial"}
function parsePostScriptName(psName) {
  const dashIdx = psName.indexOf("-");
  if (dashIdx >= 0) {
    const family = psName.slice(0, dashIdx);
    const suffix = psName.slice(dashIdx + 1).replace(/MT$/, "");
    return { family, styleOverride: STYLE_KEYWORDS[suffix] ?? null };
  }
  const commaIdx = psName.indexOf(",");
  if (commaIdx >= 0) {
    const suffix = psName.slice(commaIdx + 1).trim();
    return { family: psName.slice(0, commaIdx), styleOverride: STYLE_KEYWORDS[suffix] ?? null };
  }
  // strip a trailing "MT" (ArialMT, TimesNewRomanPSMT) to expose the base family
  const base = psName.replace(/(PS)?MT$/, "");
  return { family: base, styleOverride: null };
}

// Map a requested PDF font name to a bundled family name (or null if unknown).
function substituteFontName(name) {
  if (typeof name !== "string" || !name) return null;
  if (FONT_SUBS[name]) return FONT_SUBS[name];
  const ps = parsePostScriptName(name);
  if (ps.family !== name && FONT_SUBS[ps.family]) return FONT_SUBS[ps.family];
  // Conservative keyword fallback for names not in the table (e.g. subset
  // prefixes like "ABCDEF+Arial" or "BAAAAA+DejaVuSerifCondensed-Bold",
  // vendor variants): pick a same-class bundled family rather than letting
  // the manager mis-resolve to garbage.
  const n = name.toLowerCase();
  if (/dejavu/.test(n)) {
    if (/mono/.test(n)) return "DejaVu Sans Mono";
    return /serif/.test(n) ? "DejaVu Serif" : "DejaVu Sans";
  }
  if (/liberation/.test(n)) {
    if (/mono/.test(n)) return "Liberation Mono";
    return /serif/.test(n) ? "Liberation Serif" : "Liberation Sans";
  }
  if (/(arial|helvetica|verdana|tahoma|segoe|calibri|frutiger)/.test(n)) return "Liberation Sans";
  if (/(times|georgia|garamond|cambria|minion|book antiqua|palatino)/.test(n)) return "Liberation Serif";
  if (/(courier|consol|mono)/.test(n)) return "Liberation Mono";
  // generic class hints last (many PS names carry Serif/Sans in the family)
  if (/serif/.test(n)) return "Liberation Serif";
  if (/sans/.test(n)) return "Liberation Sans";
  return null;
}

// ── Name-resolution patch (the ASCW3 fix) ────────────────────────────────
// All JS-side font-name resolution funnels through g_fontApplication
// .GetFontFileWeb(name): unknown names go to the selection scorer, which is
// data-driven by g_fonts_selection_bin — EMPTY in our build — so every
// unknown name (embedded subset names like "BAAAAA+DejaVuSerifCondensed-
// Bold", or defaults like "Arial" that we don't bundle) resolves to ASCW3,
// ONLYOFFICE's ~10-glyph checkbox/bullet mini-font. Text shaped with ASCW3
// renders as .notdef boxes. Worse, the result is cached per name in
// FontPickerMap. Redirect any ASCW3 resolution to a same-class bundled
// family and fix the cache entry.
function installNameResolutionPatch() {
  const app = window.AscFonts && window.AscFonts.g_fontApplication;
  if (!app || typeof app.GetFontFileWeb !== "function" || app.__nameResolutionPatched) return;

  const orig = app.GetFontFileWeb.bind(app);
  app.GetFontFileWeb = function (name, lStyle) {
    let font = orig(name, lStyle);
    if (font && font.m_wsFontName === "ASCW3" && name !== "ASCW3") {
      const mapped = substituteFontName(name) || "Liberation Sans";
      const better = orig(mapped, lStyle);
      if (better && better.m_wsFontName !== "ASCW3") {
        try { app.FontPickerMap[name] = better; } catch { /* cache fix best-effort */ }
        console.log(`[fonts] name-resolution: "${name}" -> "${better.m_wsFontName}" (statt ASCW3)`);
        font = better;
      }
    }
    return font;
  };
  app.__nameResolutionPatched = true;
  console.log("[fonts] name-resolution patch installed (GetFontFileWeb)");
}

function installFontSubstitutionPatch() {
  const af = window.AscFonts;
  if (!af || typeof af.pickFont !== "function" || af.__substPatched) return false;

  const origPickFont = af.pickFont;

  if (af.jh && af.kh) {
    // ── Viewer build: resolve via the jh/kh name index directly. ──
    af.pickFont = function (name, style) {
      const kh = af.kh, jh = af.jh;
      let resolvedName = name, effectiveStyle = style;
      if (kh[resolvedName] === undefined) {
        const ps = parsePostScriptName(name);
        if (ps.styleOverride !== null) { resolvedName = ps.family; effectiveStyle = ps.styleOverride; }
      }
      if (kh[resolvedName] === undefined && FONT_SUBS[resolvedName]) resolvedName = FONT_SUBS[resolvedName];
      if (kh[resolvedName] === undefined) {
        const upper = resolvedName.toUpperCase();
        for (const k of Object.keys(kh)) { if (k.toUpperCase() === upper) { resolvedName = k; break; } }
      }
      if (kh[resolvedName] === undefined && FONT_SUBS[name]) resolvedName = FONT_SUBS[name];
      if (kh[resolvedName] !== undefined) {
        const entry = jh[kh[resolvedName]];
        if (entry && entry.Mn) return entry.Mn(AscCommon.je, effectiveStyle).file;
      }
      return origPickFont.call(this, name, style);
    };
    af.__substPatched = true;
    console.log("[fonts] substitution patch installed (viewer/jh-kh build)");
    return true;
  }

  // ── Editor build: rewrite the name, let GetFontInfo resolve the family. ──
  af.pickFont = function (name, style) {
    const mapped = substituteFontName(name);
    return origPickFont.call(this, mapped !== null ? mapped : name, style);
  };
  af.__substPatched = true;
  console.log("[fonts] substitution patch installed (editor/g_fontApplication build)");
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
  installNameResolutionPatch();
  installInputDiagnostics();
  installLongActionWatchdog();
  setStatus("Bereit. Öffne eine PDF-Datei zum Bearbeiten.");
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
    console.log(`[action-debug] StartAction type=${type} id=${id}`);
    const key = `${type}:${id}`;
    const entry = outstanding.get(key) || { type, id, count: 0, since: 0 };
    entry.count++;
    entry.since = Date.now();
    outstanding.set(key, entry);
    return origStart(type, id, actionRestriction);
  };
  editor.sync_EndAction = function (type, id, actionRestriction) {
    console.log(`[action-debug] EndAction type=${type} id=${id}`);
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

// Temporary diagnostics for tracking down why typed characters don't reach
// the document: log DOM focus changes, raw keydown events (with what currently
// has focus), and every call into the API method that actually inserts typed
// text (asc_enterText). Cheap, and removed once the input path is confirmed
// working end-to-end.
function installInputDiagnostics() {
  document.addEventListener("focus", (e) => {
    const t = e.target;
    console.log(`[input-debug] DOM focus -> <${t.tagName}${t.id ? "#" + t.id : ""}> contentEditable=${t.contentEditable}`);
  }, true);
  document.addEventListener("blur", (e) => {
    const t = e.target;
    console.log(`[input-debug] DOM blur <- <${t.tagName}${t.id ? "#" + t.id : ""}>`);
  }, true);
  window.addEventListener("keydown", (e) => {
    const a = document.activeElement;
    let longAction = "?", canEdit = "?";
    try { longAction = editor.isLongAction(); } catch { /* ignore */ }
    try { canEdit = editor.canEdit(); } catch { /* ignore */ }
    console.log(`[input-debug] keydown key="${e.key}" activeElement=<${a && a.tagName}${a && a.id ? "#" + a.id : ""}> isLongAction=${longAction} (counter=${editor.IsLongActionCurrent}) canEdit=${canEdit}`);
  }, true);
  try {
    const orig = editor.asc_enterText;
    if (typeof orig === "function") {
      editor.asc_enterText = function (codePoints, isFromPaste) {
        console.log(`[input-debug] asc_enterText called, codePoints=`, codePoints);
        // PDF subset fonts can carry a LYING cmap: it maps codepoints the
        // subset never embedded (glyph outline stripped), so the per-glyph
        // fallback never triggers ("font has it") and the char renders as a
        // box. Typing must therefore never insert INTO an embedded/subset
        // font: switch the run font to the closest bundled family first —
        // the same behaviour desktop Word processors use for missing glyphs.
        try { retargetEmbeddedRunFont(); } catch (e2) { console.warn("[fonts] retarget fehlgeschlagen:", e2); }
        return orig.apply(this, arguments);
      };
      console.log("[input-debug] instrumented asc_enterText");
    } else {
      console.warn("[input-debug] asc_enterText not found on editor — cannot instrument");
    }
  } catch (e) {
    console.warn("[input-debug] failed to instrument asc_enterText", e);
  }

  // ── Glyph-fallback probes ─────────────────────────────────────────────
  // Trace the per-glyph substitution chain that decides between a real glyph
  // and a .notdef box. Deduplicated per (codepoint → result) so the logs stay
  // complete AND readable — no fixed budget that silently runs out.
  try {
    const picker = window.AscFonts && window.AscFonts.FontPickerByCharacter;
    if (picker) {
      console.log(`[font-debug] picker ranges loaded: ${picker.Ranges.length}`);
      const seen = new Set();
      const origGet = picker.getFontBySymbol;
      picker.getFontBySymbol = function (ch) {
        const name = origGet.call(this, ch);
        const key = `${ch}:${name}`;
        if (!seen.has(key)) {
          seen.add(key);
          console.log(`[font-debug] picker: U+${(ch || 0).toString(16)} -> "${name}"`);
        }
        return name;
      };
    } else {
      console.warn("[font-debug] FontPickerByCharacter fehlt");
    }
    const tm = window.AscCommon && window.AscCommon.g_oTextMeasurer;
    if (tm && typeof tm.GetFontBySymbol === "function") {
      const seen2 = new Set();
      const origTm = tm.GetFontBySymbol;
      tm.GetFontBySymbol = function (codePoint, oPreferredFont, isForce) {
        const res = origTm.call(this, codePoint, oPreferredFont, isForce);
        const fam = res && res.Font && res.Font.m_pFaceInfo ? res.Font.m_pFaceInfo.family_name : String(res && res.Font);
        const key = `${codePoint}:${fam}`;
        if (!seen2.has(key)) {
          seen2.add(key);
          console.log(`[font-debug] measurer: U+${(codePoint || 0).toString(16)} -> font=${fam}`);
        }
        return res;
      };
      console.log("[font-debug] instrumented g_oTextMeasurer.GetFontBySymbol");
    } else {
      console.warn("[font-debug] g_oTextMeasurer.GetFontBySymbol fehlt");
    }
  } catch (e) {
    console.warn("[font-debug] Sonden-Installation fehlgeschlagen:", e);
  }
}

// Is this font name an embedded/subset PDF font? Covers the SDK's embedded
// registry prefix ("Embedded: BAAAAA+Family HASH") and bare subset-tagged
// names ("BAAAAA+Family").
function isEmbeddedFontName(name) {
  if (typeof name !== "string") return false;
  if (name.startsWith("Embedded: ")) return true;
  return /^[A-Z]{6}\+/.test(name);
}

// Strip embedded markers down to the underlying family name:
// "Embedded: BAAAAA+DejaVuSerifCondensed-Bold 7A19CC…D1" → "DejaVuSerifCondensed-Bold"
function cleanEmbeddedFontName(name) {
  return name
    .replace(/^Embedded: /, "")
    .replace(/\s+[0-9A-Fa-f]{16,}$/, "")
    .replace(/^[A-Z]{6}\+/, "");
}

// Before inserting typed text: if the cursor's run uses an embedded/subset
// font, retarget the run to the closest bundled family via the API's own
// put_TextPrFontName (which also loads the font). Self-limiting: after the
// first retarget the run font no longer matches the embedded pattern.
function retargetEmbeddedRunFont() {
  if (typeof editor.get_TextProps !== "function" || typeof editor.put_TextPrFontName !== "function") return;
  const props = editor.get_TextProps();
  const textPr = props && (props.TextPr || (typeof props.get_TextPr === "function" && props.get_TextPr()));
  const family = textPr && textPr.FontFamily && textPr.FontFamily.Name;
  if (!family || !isEmbeddedFontName(family)) return;

  const clean = cleanEmbeddedFontName(family);
  const mapped = substituteFontName(clean) || "Liberation Sans";
  console.log(`[fonts] neue Eingabe in Embedded-Font "${family}" — Run-Font wird auf "${mapped}" umgestellt`);
  editor.put_TextPrFontName(mapped);
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
    enableEditing(true);
    setStatus(`„${lastName}" geöffnet — bereit zum Bearbeiten.`);
    ensureEditorThumbnails();
    refreshHistoryButtons();
    preloadFallbackFonts();
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
  "textbox":     () => {
    if (typeof editor.AddFreeTextAnnot === "function") editor.AddFreeTextAnnot(annotType("FreeText") || 2);
    setActiveTool("textbox");
    console.log(`[input-debug] after AddFreeTextAnnot: IsLongActionCurrent=${editor.IsLongActionCurrent}`);
  },
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

  // ONLYOFFICE's text-input layer (common/text_input2.js) installs a global
  // document "focus" listener: whenever DOM focus lands on an element it does
  // not recognise as editable, it treats that as "left the editor" and disables
  // key-event capture. Our plain <button> toolbar steals focus on click, which
  // silently kills typing in text boxes/annotations afterwards — the box gets
  // created (mouse events still work), but no keystroke ever reaches it.
  // Standard fix (every WYSIWYG toolbar does this): stop the click's implicit
  // focus shift on mousedown. The click event itself still fires normally.
  el("toolbar").addEventListener("mousedown", (e) => {
    if (e.target.closest("button")) e.preventDefault();
  });

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
