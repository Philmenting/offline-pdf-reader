// Offline OCR (Texterkennung) for scanned PDFs, built on tesseract.js with
// all assets vendored under /vendor/ocr (npm run fetch-ocr) — no network at
// runtime, matching the app's offline guarantee.
//
// Two consumers share the recognition pipeline (render page range → bitmaps
// via the engine's offscreen renderer → tesseract):
//   • makeSearchablePdf(): embeds every recognized word as an INVISIBLE text
//     layer at its bounding box (pdf-lib, opacity 0), so search/select/copy
//     work in scans — the visual page stays untouched.
//   • exportRecognizedText(): plain .txt download of the recognized text.
import { loadScript, downloadBytes } from "./dom.js";

const OCR_BASE = "/vendor/ocr";
const OCR_DPI = 300;

let deps = null; // { getEditor, isDocOpen, showPromptDialog, parsePageRangeSpec,
                 //   getDocName, setStatus, renderer, collectPdfBytes,
                 //   openArrayBuffer, loadPdfLib, markClean }
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

// Prompt for a page range and recognize it. Returns null (user cancelled /
// unavailable) or [{ nPage, text, words, widthPx, heightPx }].
async function recognizePages(purposeLine) {
  if (!deps.isDocOpen()) return null;
  const { setStatus } = deps;

  if (!(await ocrAvailable())) {
    setStatus(`OCR-Daten fehlen in diesem Build (vendor/ocr — siehe „npm run fetch-ocr").`);
    return null;
  }

  const editor = deps.getEditor();
  const pageCount = editor.getCountPages() | 0;
  const spec = await deps.showPromptDialog(
    `${purposeLine}\nWelche Seiten? z.B. "1-3" — Dokument hat ${pageCount} Seite(n).`,
    `1-${pageCount}`
  );
  if (!spec) return null;

  let indexes;
  try {
    indexes = deps.parsePageRangeSpec(spec, pageCount);
  } catch (e) {
    setStatus(`OCR fehlgeschlagen: ${e.message}`);
    return null;
  }

  const r = deps.renderer();
  const doc = editor.getPDFDoc();
  if (!r || typeof r.GetPrintPage !== "function") {
    setStatus("OCR nicht verfügbar (Renderer fehlt).");
    return null;
  }
  setStatus("OCR-Modell wird geladen …");
  const worker = await ensureWorker();

  const results = [];
  for (let i = 0; i < indexes.length; i++) {
    const nPage = indexes[i];
    setStatus(`Texterkennung: Seite ${nPage + 1} (${i + 1}/${indexes.length}) …`);
    const widthPx = Math.round(doc.GetPageWidthMM(nPage) / 25.4 * OCR_DPI);
    const heightPx = Math.round(doc.GetPageHeightMM(nPage) / 25.4 * OCR_DPI);
    const canvas = r.GetPrintPage(nPage, widthPx, heightPx,
      window.AscPDF.PRINT_CONTENT_TYPES.docAndMarkups);
    // tesseract.js v6 omits word geometry unless the blocks output is
    // explicitly requested — without it the searchable layer stays empty.
    const { data } = await worker.recognize(canvas, {}, { text: true, blocks: true });
    results.push({
      nPage,
      text: (data.text || "").trim(),
      words: collectWords(data),
      widthPx, heightPx,
    });
  }
  return results;
}

// tesseract.js v6 nests words under blocks→paragraphs→lines; older builds
// expose data.words directly. Accept both.
function collectWords(data) {
  if (Array.isArray(data.words) && data.words.length) return data.words;
  const words = [];
  for (const block of data.blocks || []) {
    for (const para of block.paragraphs || []) {
      for (const line of para.lines || []) {
        for (const w of line.words || []) words.push(w);
      }
    }
  }
  return words;
}

/** Embed the recognized words as an invisible, searchable text layer. */
export async function makeSearchablePdf() {
  const { setStatus } = deps;
  try {
    const results = await recognizePages("PDF durchsuchbar machen (OCR, Deutsch/Englisch):");
    if (!results) return;

    const totalWords = results.reduce((n, r) => n + r.words.length, 0);
    if (!totalWords) {
      setStatus("OCR abgeschlossen — kein Text erkannt, nichts einzubetten.");
      return;
    }

    setStatus("Unsichtbare Textebene wird eingebettet …");
    const bytes = await deps.collectPdfBytes(); // current state incl. edits
    if (!bytes) {
      setStatus("OCR fehlgeschlagen: keine gültigen PDF-Daten von der Engine.");
      return;
    }
    await deps.loadPdfLib();
    const { PDFDocument, StandardFonts } = window.PDFLib;
    const pdf = await PDFDocument.load(bytes);
    const font = await pdf.embedFont(StandardFonts.Helvetica);
    const pages = pdf.getPages();

    let embedded = 0;
    for (const res of results) {
      const page = pages[res.nPage];
      if (!page) continue;
      const scale = page.getWidth() / res.widthPx; // px @300dpi → pt
      const pageH = page.getHeight();
      for (const w of res.words) {
        const text = (w.text || "").trim();
        const b = w.bbox;
        if (!text || !b) continue;
        const size = Math.max(4, (b.y1 - b.y0) * scale * 0.85);
        try {
          page.drawText(text, {
            x: b.x0 * scale,
            y: pageH - b.y1 * scale + size * 0.18, // approximate baseline
            size, font,
            opacity: 0, // invisible, but searchable/selectable
          });
          embedded++;
        } catch { /* glyphs outside WinAnsi — skip the word */ }
      }
    }
    if (!embedded) {
      setStatus("OCR abgeschlossen — erkannte Wörter konnten nicht eingebettet werden.");
      return;
    }
    const outBytes = await pdf.save();
    deps.markClean(); // new bytes carry the full state — skip reopen confirm
    setStatus(`Durchsuchbare Textebene eingebettet: ${embedded} Wörter auf ${results.length} Seite(n) — bitte speichern.`);
    deps.openArrayBuffer(outBytes.buffer, deps.getDocName());
  } catch (e) {
    console.error("OCR fehlgeschlagen:", e);
    setStatus(`OCR fehlgeschlagen: ${e.message}`);
  }
}

/** Recognize text and hand it to the user as a .txt download. */
export async function exportRecognizedText() {
  const { setStatus } = deps;
  try {
    const results = await recognizePages("Texterkennung (OCR, Deutsch/Englisch), Export als Textdatei:");
    if (!results) return;

    const text = results.map((r) => `── Seite ${r.nPage + 1} ──\n${r.text}`).join("\n\n");
    if (!text.replace(/── Seite \d+ ──/g, "").trim()) {
      setStatus("OCR abgeschlossen — kein Text erkannt.");
      return;
    }
    const outName = deps.getDocName().replace(/\.pdf$/i, "") + "-OCR.txt";
    downloadBytes(new TextEncoder().encode(text), outName, "text/plain;charset=utf-8");
    setStatus(`OCR abgeschlossen: „${outName}" (${results.length} Seite(n)).`);
  } catch (e) {
    console.error("OCR fehlgeschlagen:", e);
    setStatus(`OCR fehlgeschlagen: ${e.message}`);
  }
}
