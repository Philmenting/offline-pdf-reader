// Signature tool: draw a signature once (mouse/touch on a canvas), keep it in
// persistent storage, and stamp it into the document as an image via the
// editor's regular image pipeline (same path as the "Bild" tool, so it
// serializes on save the same way).
import { el } from "./dom.js";
import { storeGet, storeSet } from "./storage.js";

const STORE_KEY = "signature-image";

let deps = null; // { insertImageDataUrl, refocusEditor, setStatus }
let drawing = false;
let hasStrokes = false;
let lastPoint = null;

function canvas() { return el("signature-canvas"); }
function ctx() { return canvas().getContext("2d"); }

function pointFromEvent(e) {
  const rect = canvas().getBoundingClientRect();
  const src = e.touches ? e.touches[0] : e;
  return {
    x: (src.clientX - rect.left) * (canvas().width / rect.width),
    y: (src.clientY - rect.top) * (canvas().height / rect.height),
  };
}

function startStroke(e) {
  drawing = true;
  lastPoint = pointFromEvent(e);
  e.preventDefault();
}

function moveStroke(e) {
  if (!drawing) return;
  const p = pointFromEvent(e);
  const c = ctx();
  c.strokeStyle = "#101a86"; // classic ink blue
  c.lineWidth = 3;
  c.lineCap = "round";
  c.lineJoin = "round";
  c.beginPath();
  c.moveTo(lastPoint.x, lastPoint.y);
  c.lineTo(p.x, p.y);
  c.stroke();
  lastPoint = p;
  hasStrokes = true;
  e.preventDefault();
}

function endStroke() { drawing = false; }

function clearCanvas() {
  ctx().clearRect(0, 0, canvas().width, canvas().height);
  hasStrokes = false;
}

// Trim the transparent margin so the stamped image is exactly the signature.
function trimmedDataUrl() {
  const c = canvas();
  const data = ctx().getImageData(0, 0, c.width, c.height);
  let minX = c.width, minY = c.height, maxX = -1, maxY = -1;
  for (let y = 0; y < c.height; y++) {
    for (let x = 0; x < c.width; x++) {
      if (data.data[(y * c.width + x) * 4 + 3] > 8) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  if (maxX < 0) return null;
  const pad = 6;
  minX = Math.max(0, minX - pad); minY = Math.max(0, minY - pad);
  maxX = Math.min(c.width - 1, maxX + pad); maxY = Math.min(c.height - 1, maxY + pad);
  const out = document.createElement("canvas");
  out.width = maxX - minX + 1;
  out.height = maxY - minY + 1;
  out.getContext("2d").drawImage(c, minX, minY, out.width, out.height, 0, 0, out.width, out.height);
  return out.toDataURL("image/png");
}

function closeDialog() {
  el("signature-dialog").hidden = true;
  deps.refocusEditor();
}

/** Entry point for the toolbar button: insert saved signature or open the pad. */
export async function insertSignature() {
  const saved = await storeGet(STORE_KEY);
  if (saved) {
    deps.insertImageDataUrl(saved);
    deps.setStatus("Signatur eingefügt — zum Verschieben/Skalieren ziehen. Neue Signatur: Knopf erneut mit gedrückter Umschalttaste.");
    return;
  }
  openSignaturePad();
}

export function openSignaturePad() {
  clearCanvas();
  el("signature-dialog").hidden = false;
}

export function wireSignatureDialog(dependencies) {
  deps = dependencies;
  const c = canvas();
  c.addEventListener("mousedown", startStroke);
  c.addEventListener("mousemove", moveStroke);
  window.addEventListener("mouseup", endStroke);
  c.addEventListener("touchstart", startStroke, { passive: false });
  c.addEventListener("touchmove", moveStroke, { passive: false });
  c.addEventListener("touchend", endStroke);

  el("signature-clear").addEventListener("click", clearCanvas);
  el("signature-cancel").addEventListener("click", closeDialog);
  el("signature-ok").addEventListener("click", async () => {
    if (!hasStrokes) { closeDialog(); return; }
    const dataUrl = trimmedDataUrl();
    if (!dataUrl) { closeDialog(); return; }
    await storeSet(STORE_KEY, dataUrl);
    closeDialog();
    deps.insertImageDataUrl(dataUrl);
    deps.setStatus("Signatur gespeichert und eingefügt — zum Verschieben/Skalieren ziehen.");
  });
  el("signature-dialog").addEventListener("click", (e) => {
    if (e.target === el("signature-dialog")) closeDialog();
  });
}
