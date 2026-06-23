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

  viewer = new window.AscViewer.CViewer("id_viewer", {
    sdkjsPath: SDKJS_PATH,
    fontsPath: FONTS_PATH,
  });
  thumbnails = viewer.createThumbnails("thumbnails");

  window.addEventListener("resize", () => {
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

function openArrayBuffer(buf, name) {
  if (!viewer) return;
  const bytes = new Uint8Array(buf);
  const magic = String.fromCharCode(...bytes.slice(0, 5));
  if (magic !== "%PDF-") {
    setStatus(`„${name}" ist keine gültige PDF-Datei.`);
    return;
  }
  el("placeholder").style.display = "none";
  viewer.open(buf);
  viewer.resize();
  enableTools(true);
  setStatus(`„${name}" geöffnet (${(bytes.length / 1024).toFixed(0)} KB).`);
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
