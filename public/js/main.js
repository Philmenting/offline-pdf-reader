/**
 * Host for the offline PDF editor, built on the ONLYOFFICE standalone viewer
 * (AscViewer.CViewer). This mirrors upstream's own sdkjs/pdf/test harness
 * (base.js) but as our own minimal UI.
 *
 * The built engine bundle `pdf/src/engine/viewer.js` already includes the
 * high-level CViewer wrapper, so a single script provides:
 *   new AscViewer.CViewer(mountId, { sdkjsPath, fontsPath, theme })
 *     .open(arrayBuffer) / .setZoom / .setZoomMode / .rotatePage
 *     .createThumbnails(panelId) / .resize() / .registerEvent(...)
 *
 * The font registry (common/AllFonts.js) and TTFs (fontsPath) are loaded
 * lazily by the engine on first open.
 */

const SDKJS_PATH = "/vendor/onlyoffice/sdkjs";
const FONTS_PATH = "/vendor/fonts/";
const ENGINE_SCRIPT = `${SDKJS_PATH}/pdf/src/engine/viewer.js`;

const ZOOM_STEPS = [50, 75, 90, 100, 110, 125, 150, 175, 200, 250, 300, 400];

// AscCommon.ViewerZoomMode enum (sdkjs/pdf/src/viewer.js). Hardcoded because
// the compiled engine bundle renames the symbol, so it isn't reachable by name.
const ZOOM_MODE = { Custom: 0, Width: 1, Page: 2 };

const el = (id) => document.getElementById(id);
const statusEl = el("status");
const setStatus = (msg) => { statusEl.textContent = msg; };

let viewer = null;
let thumbnails = null;

// The engine's viewer.js does `"" != window.g_fonts_selection_bin` and then
// base64-decodes it; if it is undefined that decode throws ("Cannot read
// properties of undefined (reading 'length')") and no page ever renders. We
// ship no precomputed font-selection table, so seed the empty-string fallback
// before the engine runs. (AllFonts.js also sets this; this is belt-and-braces
// so older font registries keep working.)
if (typeof window["g_fonts_selection_bin"] === "undefined") {
  window["g_fonts_selection_bin"] = "";
}

// ── Diagnostics ───────────────────────────────────────────────────────────
const diag = document.createElement("div");
diag.style.cssText =
  "position:fixed;right:8px;bottom:32px;max-width:46ch;max-height:50vh;overflow:auto;" +
  "z-index:9999;background:rgba(20,24,34,.92);color:#e7ecf5;font:11px/1.4 monospace;" +
  "padding:8px 10px;border-radius:8px;white-space:pre-wrap;pointer-events:none;display:none;";
document.body.appendChild(diag);
function diagLog(line, isError) {
  const t = new Date().toLocaleTimeString();
  const row = document.createElement("div");
  if (isError) { row.style.color = "#ff9b9b"; diag.style.display = "block"; }
  row.textContent = `${t}  ${line}`;
  diag.appendChild(row);
  diag.scrollTop = diag.scrollHeight;
  (isError ? console.error : console.log)(`[diag] ${line}`);
}
window.addEventListener("error", (e) => {
  diagLog(`window.onerror: ${e.message} @ ${e.filename}:${e.lineno}:${e.colno}`, true);
  if (e.error && e.error.stack) diagLog(String(e.error.stack), true);
});
window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason;
  diagLog(`unhandledrejection: ${(r && r.message) || r}`, true);
  if (r && r.stack) diagLog(String(r.stack), true);
});

// ── Font pipeline instrumentation ────────────────────────────────────────
// Intercepts XHR to log every font/resource fetch and its outcome.
const _xhrOpen = XMLHttpRequest.prototype.open;
const _xhrSend = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open = function (method, url, ...rest) {
  this._diagUrl = String(url);
  return _xhrOpen.call(this, method, url, ...rest);
};
XMLHttpRequest.prototype.send = function (...args) {
  const url = this._diagUrl || "";
  if (url.includes("/fonts/") || url.includes("AllFonts") || url.includes("cmap") || url.includes(".ttf") || url.includes(".otf")) {
    diagLog(`XHR → ${url.split("/").pop()}`);
    this.addEventListener("load", () => {
      const size = this.response ? (this.response.byteLength || this.response.length || 0) : 0;
      diagLog(`XHR ✓ ${url.split("/").pop()} ${this.status} (${(size/1024).toFixed(0)}KB)`);
    });
    this.addEventListener("error", () => {
      diagLog(`XHR ✗ ${url.split("/").pop()} NETWORK ERROR`, true);
    });
    this.addEventListener("timeout", () => {
      diagLog(`XHR ✗ ${url.split("/").pop()} TIMEOUT`, true);
    });
  }
  return _xhrSend.apply(this, args);
};

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

async function initEngine() {
  setStatus("PDF-Engine wird geladen …");
  await loadScript(ENGINE_SCRIPT);
  if (!(window.AscViewer && typeof window.AscViewer.CViewer === "function")) {
    throw new Error("Engine geladen, aber AscViewer.CViewer fehlt.");
  }

  viewer = new window.AscViewer.CViewer("viewer-container", {
    sdkjsPath: SDKJS_PATH,
    fontsPath: FONTS_PATH,
  });
  thumbnails = viewer.createThumbnails("thumbnails");

  // Surface engine lifecycle events so we can see where opening/rendering stops.
  const reg = (name) => {
    try {
      viewer.registerEvent(name, (...args) => {
        let detail = "";
        try { detail = args.length ? JSON.stringify(args[0]).slice(0, 120) : ""; } catch { detail = "[unserializable]"; }
        diagLog(`event ${name} ${detail}`);
      });
    } catch (e) { diagLog(`registerEvent(${name}) failed: ${e.message}`, true); }
  };
  ["onFileOpened", "onPagesCount", "onNeedPassword", "onStructure", "onCurrentPageChanged", "onZoom", "onRepaint"].forEach(reg);
  diagLog("viewer created; AllFonts at " + `${SDKJS_PATH}/common/AllFonts.js`);
  diagLog(`__fonts_files=${(window.__fonts_files||[]).length} __fonts_infos=${(window.__fonts_infos||[]).length} g_fonts_selection_bin=${typeof window.g_fonts_selection_bin}(${(window.g_fonts_selection_bin||"").length})`);

  if (typeof window.AscViewer.checkApplicationScale === "function") {
    window.AscViewer.checkApplicationScale();
  }

  window.addEventListener("resize", () => {
    if (typeof window.AscViewer.checkApplicationScale === "function") {
      window.AscViewer.checkApplicationScale();
    }
    viewer && viewer.resize();
    thumbnails && thumbnails.resize();
  });

  // The engine requires the font registry (common/AllFonts.js) to open any PDF.
  // Preflight it so we can give an actionable message instead of a silent fail.
  const fontsOk = await fetch(`${SDKJS_PATH}/common/AllFonts.js`, { method: "HEAD" })
    .then((r) => r.ok)
    .catch(() => false);
  if (!fontsOk) {
    setStatus("Engine bereit – aber Font-Registry fehlt: vendor/onlyoffice/sdkjs/common/AllFonts.js (siehe README).");
    return;
  }

  setStatus("Bereit. Öffne eine PDF-Datei.");
}

function enableTools(on) {
  for (const id of ["btn-zoom-out", "btn-zoom-in", "btn-fit-width", "btn-fit-page", "btn-rotate-left", "btn-rotate-right"]) {
    el(id).disabled = !on;
  }
}

function currentZoom() {
  return viewer ? Math.round(viewer.getZoom()) : 100;
}

function stepZoom(dir) {
  const z = currentZoom();
  if (dir > 0) {
    const next = ZOOM_STEPS.find((v) => v > z);
    if (next) viewer.setZoom(next);
  } else {
    const below = [...ZOOM_STEPS].reverse().find((v) => v < z);
    if (below) viewer.setZoom(below);
  }
  setStatus(`Zoom: ${currentZoom()} %`);
}

function inspectRenderState(label) {
  try {
    const eng = viewer.getEngine();
    const file = eng.file || eng.z || eng;
    const pageCount = typeof file.Na === "function" ? file.Na() : "?";
    let pendingFonts = "?";
    try {
      const pages = file.l || file.pages || [];
      if (pages.length > 0) {
        const p0 = pages[0];
        pendingFonts = (p0.fonts || p0.Oe || []).length;
      }
    } catch {}
    const cvs = document.getElementById("id_viewer");
    const cvsInfo = cvs ? `${cvs.width}x${cvs.height}` : "MISSING";
    let nonWhite = 0;
    if (cvs && cvs.getContext) {
      try {
        const ctx = cvs.getContext("2d");
        const d = ctx.getImageData(0, 0, cvs.width, cvs.height).data;
        for (let i = 0; i < d.length; i += 4) {
          if (d[i+3] > 0 && !(d[i] > 250 && d[i+1] > 250 && d[i+2] > 250)) nonWhite++;
        }
      } catch {}
    }
    diagLog(`[${label}] pages=${pageCount} pendingFonts=${pendingFonts} canvas=${cvsInfo} nonWhitePx=${nonWhite}`);
    // Log AscFonts state
    if (window.AscFonts) {
      const af = window.AscFonts;
      diagLog(`[${label}] AscFonts: pickFont=${typeof af.pickFont} files=${(window.__fonts_files||[]).length} infos=${(window.__fonts_infos||[]).length}`);
    }
  } catch (e) {
    diagLog(`[${label}] inspect error: ${e.message}`, true);
  }
}

function openArrayBuffer(buf, name) {
  if (!viewer) return;
  const bytes = new Uint8Array(buf);
  const magic = String.fromCharCode(...bytes.slice(0, 5));
  if (magic !== "%PDF-") {
    setStatus(`„${name}" ist keine gültige PDF-Datei.`);
    return;
  }
  el("placeholder").style.display = "none";
  diagLog(`open() called for "${name}" (${(bytes.length / 1024).toFixed(0)} KB)`);
  // Log pre-open state
  diagLog(`g_fonts_selection_bin="${typeof window.g_fonts_selection_bin}" __fonts_files=${(window.__fonts_files||[]).length} __fonts_infos=${(window.__fonts_infos||[]).length}`);
  try {
    viewer.open(buf);
  } catch (e) {
    diagLog(`open() threw: ${e.message}`, true);
    if (e.stack) diagLog(String(e.stack), true);
  }
  enableTools(true);
  setStatus(`„${name}" geöffnet (${(bytes.length / 1024).toFixed(0)} KB).`);
  // Inspect render state at intervals to see font loading progress
  setTimeout(() => inspectRenderState("1s"), 1000);
  setTimeout(() => inspectRenderState("3s"), 3000);
  setTimeout(() => inspectRenderState("8s"), 8000);
  setTimeout(() => inspectRenderState("15s"), 15000);
}

function onFileChosen(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => openArrayBuffer(reader.result, file.name);
  reader.onerror = () => setStatus("Datei konnte nicht gelesen werden.");
  reader.readAsArrayBuffer(file);
}

function wireUi() {
  el("file-input").addEventListener("change", (e) => onFileChosen(e.target.files[0]));
  el("btn-zoom-in").addEventListener("click", () => stepZoom(1));
  el("btn-zoom-out").addEventListener("click", () => stepZoom(-1));
  el("btn-fit-width").addEventListener("click", () => viewer.setZoomMode(ZOOM_MODE.Width));
  el("btn-fit-page").addEventListener("click", () => viewer.setZoomMode(ZOOM_MODE.Page));
  el("btn-rotate-left").addEventListener("click", () => viewer.rotatePage(undefined, -90, true));
  el("btn-rotate-right").addEventListener("click", () => viewer.rotatePage(undefined, 90, true));

  // Drag & drop onto the viewer.
  const host = document.querySelector(".viewer-host");
  host.addEventListener("dragover", (e) => { e.preventDefault(); });
  host.addEventListener("drop", (e) => {
    e.preventDefault();
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    onFileChosen(f);
  });
}

wireUi();
initEngine().catch((err) => {
  console.error(err);
  setStatus(`Fehler beim Laden der Engine: ${err.message}`);
});
