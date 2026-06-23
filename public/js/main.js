/**
 * Host bootstrap for the offline PDF editor.
 *
 * Loads the vendored ONLYOFFICE PDF engine and reports its status. Wiring the
 * full editor API (Asc.PDFEditorApi) that drives AscViewer.CViewer is the next
 * milestone; see README "Key coupling constraint".
 */

const ENGINE_BASE = "/vendor/onlyoffice/pdf/src/engine/";
const statusEl = document.getElementById("status");
const fileInput = document.getElementById("file-input");

function setStatus(msg) {
  statusEl.textContent = msg;
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.async = false;
    s.onload = () => resolve(src);
    s.onerror = () => reject(new Error(`Failed to load ${src}`));
    document.head.appendChild(s);
  });
}

async function loadEngine() {
  setStatus("PDF-Engine wird geladen …");
  // The engine reads these to locate its WASM + cmap relative to the host.
  window.AscViewer = window.AscViewer || {};
  window.AscViewer.baseUrl = ENGINE_BASE;
  window.AscViewer.baseEngineUrl = ENGINE_BASE;

  await loadScript(ENGINE_BASE + "drawingfile.js");
  await loadScript(ENGINE_BASE + "viewer.js");

  const hasViewer = !!(window.AscViewer && window.AscViewer.CViewer);
  const hasCommon = !!(window.AscCommon && window.AscCommon.CViewer);
  if (!hasViewer && !hasCommon) {
    throw new Error("Engine geladen, aber AscViewer.CViewer fehlt.");
  }
  setStatus("PDF-Engine bereit. Editor-API-Anbindung folgt (siehe README).");
  return { hasViewer, hasCommon };
}

function onFileChosen(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    const bytes = new Uint8Array(reader.result);
    // Sanity check the PDF magic header.
    const magic = String.fromCharCode(...bytes.slice(0, 5));
    if (magic !== "%PDF-") {
      setStatus(`„${file.name}" ist keine gültige PDF-Datei.`);
      return;
    }
    document.getElementById("placeholder").style.display = "none";
    setStatus(
      `„${file.name}" geladen (${(bytes.length / 1024).toFixed(0)} KB). ` +
      `Rendering folgt mit der Editor-API-Anbindung.`
    );
    // TODO(next milestone): construct Asc.PDFEditorApi, then
    // new AscViewer.CViewer("id_viewer", api) and open these bytes.
    window.__pdfBytes = bytes;
  };
  reader.onerror = () => setStatus("Datei konnte nicht gelesen werden.");
  reader.readAsArrayBuffer(file);
}

fileInput.addEventListener("change", (e) => onFileChosen(e.target.files[0]));

loadEngine().catch((err) => {
  console.error(err);
  setStatus(`Fehler beim Laden der Engine: ${err.message}`);
});
