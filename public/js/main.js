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

const ZOOM_MODE = { Custom: 0, Width: 1, Page: 2 };

const el = (id) => document.getElementById(id);
const statusEl = el("status");
const setStatus = (msg) => { statusEl.textContent = msg; };

let viewer = null;
let thumbnails = null;

if (typeof window["g_fonts_selection_bin"] === "undefined") {
  window["g_fonts_selection_bin"] = "";
}

// ── Font substitution ────────────────────────────────────────────────────
// The engine's font manager scores fonts via g_fonts_selection_bin. We ship
// no precomputed binary, so the scoring index only contains the minimal ASCW3
// fallback. We patch pickFont to do a direct name lookup with a substitution
// table mapping common font names to our bundled Liberation/DejaVu families.
function installFontSubstitutionPatch() {
  const af = window.AscFonts;
  if (!af || !af.jh || !af.kh) return false;

  const SUBS = {
    "Arial": "Liberation Sans",
    "Arial Narrow": "Liberation Sans Narrow",
    "Helvetica": "Liberation Sans",
    "Helvetica Neue": "Liberation Sans",
    "Times New Roman": "Liberation Serif",
    "Times": "Liberation Serif",
    "Times Roman": "Liberation Serif",
    "TimesNewRoman": "Liberation Serif",
    "TimesNewRomanPS": "Liberation Serif",
    "TimesNewRomanPSMT": "Liberation Serif",
    "ArialMT": "Liberation Sans",
    "CourierNewPSMT": "Liberation Mono",
    "Courier New": "Liberation Mono",
    "Courier": "Liberation Mono",
    "Calibri": "Carlito",
    "Cambria": "Caladea",
    "Verdana": "DejaVu Sans",
    "Georgia": "DejaVu Serif",
    "Tahoma": "DejaVu Sans",
    "Trebuchet MS": "DejaVu Sans",
    "Lucida Sans": "DejaVu Sans",
    "Lucida Console": "DejaVu Sans Mono",
    "Consolas": "DejaVu Sans Mono",
    "Segoe UI": "DejaVu Sans",
    "Palatino": "DejaVu Serif",
    "Palatino Linotype": "DejaVu Serif",
    "Book Antiqua": "DejaVu Serif",
    "Garamond": "DejaVu Serif",
    "Century": "DejaVu Serif",
    "Impact": "Liberation Sans",
    "Comic Sans MS": "DejaVu Sans",
    "Symbol": "Symbola",
    "ZapfDingbats": "Symbola",
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
    const kh = af.kh;
    const jh = af.jh;

    let resolvedName = name;
    let effectiveStyle = style;

    if (kh[resolvedName] === undefined) {
      const ps = parsePostScriptName(name);
      if (ps.styleOverride !== null) {
        resolvedName = ps.family;
        effectiveStyle = ps.styleOverride;
      }
    }

    if (kh[resolvedName] === undefined && SUBS[resolvedName]) {
      resolvedName = SUBS[resolvedName];
    }
    if (kh[resolvedName] === undefined) {
      const upper = resolvedName.toUpperCase();
      for (const k of Object.keys(kh)) {
        if (k.toUpperCase() === upper) { resolvedName = k; break; }
      }
    }
    if (kh[resolvedName] === undefined && SUBS[name]) {
      resolvedName = SUBS[name];
    }

    if (kh[resolvedName] !== undefined) {
      const entry = jh[kh[resolvedName]];
      if (entry && entry.Mn) {
        return entry.Mn(AscCommon.je, effectiveStyle).file;
      }
    }

    return origPickFont.call(this, name, style);
  };

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
  try {
    viewer.open(buf);
  } catch (e) {
    console.error("open() error:", e);
  }
  installFontSubstitutionPatch();
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
