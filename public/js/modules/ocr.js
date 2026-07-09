// Offline OCR (Texterkennung) for scanned PDFs, built on tesseract.js with
// all assets vendored under /vendor/ocr (npm run fetch-ocr) — no network at
// runtime, matching the app's offline guarantee.
//
// Flow: render the chosen page range to bitmaps via the engine's offscreen
// renderer (GetPrintPage, the same path the PNG export uses), recognize each
// bitmap (German + English models), and hand the user the combined text as a
// .txt download. It never modifies the document.
import { loadScript, downloadBytes } from "./dom.js";

const OCR_BASE = "/vendor/ocr";
const OCR_DPI = 300;

let deps = null; // { getEditor, isDocOpen, showPromptDialog, parsePageRangeSpec, getDocName, setStatus, renderer }
let workerPromise = null;

async function ocrAvailable() {
  try {
    const res = await fetch(`${OCR_BASE}/tesseract.min.js`, { method: "HEAD" });
    return res.ok;
  } catch { return false; }
}

function ensureWorker() {
  if (!workerPromise) {
    workerPromise = (async () => {
      if (!window.Tesseract) await loadScript(`${OCR_BASE}/tesseract.min.js`);
      return window.Tesseract.createWorker(["deu", "eng"], 1, {
        workerPath: `${OCR_BASE}/worker.min.js`,
        corePath: `${OCR_BASE}/core`,
        langPath: `${OCR_BASE}/lang`,
        gzip: true,
      });
    })();
    workerPromise.catch(() => { workerPromise = null; }); // allow retry
  }
  return workerPromise;
}

export function initOcr(dependencies) {
  deps = dependencies;
}

export async function recognizeText() {
  if (!deps.isDocOpen()) return;
  const { setStatus } = deps;

  if (!(await ocrAvailable())) {
    setStatus(`OCR-Daten fehlen in diesem Build (vendor/ocr — siehe „npm run fetch-ocr").`);
    return;
  }

  const editor = deps.getEditor();
  const pageCount = editor.getCountPages() | 0;
  const spec = await deps.showPromptDialog(
    `Texterkennung (OCR, Deutsch/Englisch):\nWelche Seiten? z.B. "1-3" — Dokument hat ${pageCount} Seite(n).`,
    `1-${pageCount}`
  );
  if (!spec) return;

  let indexes;
  try {
    indexes = deps.parsePageRangeSpec(spec, pageCount);
  } catch (e) {
    setStatus(`OCR fehlgeschlagen: ${e.message}`);
    return;
  }

  try {
    const r = deps.renderer();
    const doc = editor.getPDFDoc();
    if (!r || typeof r.GetPrintPage !== "function") {
      setStatus("OCR nicht verfügbar (Renderer fehlt).");
      return;
    }
    setStatus("OCR-Modell wird geladen …");
    const worker = await ensureWorker();

    const parts = [];
    for (let i = 0; i < indexes.length; i++) {
      const nPage = indexes[i];
      setStatus(`Texterkennung: Seite ${nPage + 1} (${i + 1}/${indexes.length}) …`);
      const widthPx = Math.round(doc.GetPageWidthMM(nPage) / 25.4 * OCR_DPI);
      const heightPx = Math.round(doc.GetPageHeightMM(nPage) / 25.4 * OCR_DPI);
      const canvas = r.GetPrintPage(nPage, widthPx, heightPx,
        window.AscPDF.PRINT_CONTENT_TYPES.docAndMarkups);
      const { data } = await worker.recognize(canvas);
      parts.push(`── Seite ${nPage + 1} ──\n${(data.text || "").trim()}`);
    }

    const text = parts.join("\n\n");
    if (!text.replace(/── Seite \d+ ──/g, "").trim()) {
      setStatus("OCR abgeschlossen — kein Text erkannt.");
      return;
    }
    const outName = deps.getDocName().replace(/\.pdf$/i, "") + "-OCR.txt";
    downloadBytes(new TextEncoder().encode(text), outName, "text/plain;charset=utf-8");
    setStatus(`OCR abgeschlossen: „${outName}" (${indexes.length} Seite(n)).`);
  } catch (e) {
    console.error("OCR fehlgeschlagen:", e);
    setStatus(`OCR fehlgeschlagen: ${e.message}`);
  }
}
