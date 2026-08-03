// Offline OCR (Texterkennung) for scanned PDFs, built on tesseract.js with
// all assets vendored under /vendor/ocr (npm run fetch-ocr) - no network at
// runtime, matching the app's offline guarantee.
//
// Two consumers share the recognition pipeline (render page range -> bitmaps
// via the engine's offscreen renderer -> tesseract):
//   * makeSearchablePdf(): embeds recognized lines as an invisible text layer,
//     so search/select/copy work across complete phrases in scans.
//   * exportRecognizedText(): plain .txt download of the recognized text.
import { loadScript, downloadBytes } from "./dom.js";

const OCR_BASE = "/vendor/ocr";
const OCR_DPI = 300;

let deps = null; // { getEditor, isDocOpen, showPromptDialog, parsePageRangeSpec,
                 //   getDocName, setStatus, renderer, collectPdfBytes,
                 //   openArrayBuffer, loadPdfLib, markClean }
let workerPromise = null;

export async function ocrStackAvailable() { return ocrAvailable(); }

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

/** Recognize a single canvas; returns word objects with bbox geometry. */
export async function recognizeCanvasWords(canvas) {
  const worker = await ensureWorker();
  const { data } = await worker.recognize(canvas, {}, { text: true, blocks: true });
  return collectWords(data);
}

function validBbox(word) {
  const b = word && word.bbox;
  return b && [b.x0, b.y0, b.x1, b.y1].every(Number.isFinite)
    && b.x1 > b.x0 && b.y1 > b.y0;
}

function wordLineKey(word) {
  if (word && word._ocrLine != null) return String(word._ocrLine);
  if (word && word.line_num != null) return String(word.line_num);
  if (word && word.line && word.line.id != null) return String(word.line.id);
  return null;
}

function lineFromWords(words) {
  const ordered = words.filter(validBbox).sort((a, b) => a.bbox.x0 - b.bbox.x0);
  if (!ordered.length) return null;
  return {
    words: ordered,
    x0: Math.min(...ordered.map((w) => w.bbox.x0)),
    y0: Math.min(...ordered.map((w) => w.bbox.y0)),
    x1: Math.max(...ordered.map((w) => w.bbox.x1)),
    y1: Math.max(...ordered.map((w) => w.bbox.y1)),
  };
}

function groupWordsIntoLines(words) {
  const explicit = new Map();
  const unassigned = [];

  for (const word of words || []) {
    const text = (word && word.text || "").trim();
    if (!text || !validBbox(word)) continue;
    const key = wordLineKey(word);
    if (key == null) unassigned.push(word);
    else {
      if (!explicit.has(key)) explicit.set(key, []);
      explicit.get(key).push(word);
    }
  }

  const lines = [...explicit.values()].map(lineFromWords).filter(Boolean);
  const geometric = [];
  const ordered = unassigned.sort((a, b) => {
    const ay = (a.bbox.y0 + a.bbox.y1) / 2;
    const by = (b.bbox.y0 + b.bbox.y1) / 2;
    return ay - by || a.bbox.x0 - b.bbox.x0;
  });

  for (const word of ordered) {
    const b = word.bbox;
    const h = b.y1 - b.y0;
    const cy = (b.y0 + b.y1) / 2;
    let best = null;
    let bestDistance = Infinity;

    for (const line of geometric) {
      const lineH = line.y1 - line.y0;
      const lineCy = (line.y0 + line.y1) / 2;
      const overlap = Math.min(b.y1, line.y1) - Math.max(b.y0, line.y0);
      const overlapRatio = overlap / Math.max(1, Math.min(h, lineH));
      const distance = Math.abs(cy - lineCy);
      if (overlapRatio >= 0.5 && distance <= Math.max(h, lineH) * 0.6 && distance < bestDistance) {
        best = line;
        bestDistance = distance;
      }
    }

    if (!best) {
      geometric.push({ words: [word], x0: b.x0, y0: b.y0, x1: b.x1, y1: b.y1 });
    } else {
      best.words.push(word);
      best.x0 = Math.min(best.x0, b.x0);
      best.y0 = Math.min(best.y0, b.y0);
      best.x1 = Math.max(best.x1, b.x1);
      best.y1 = Math.max(best.y1, b.y1);
    }
  }

  // Geometry-only OCR output can put two columns on the same baseline. Split
  // those at a gap that is far wider than a normal inter-word space.
  for (const line of geometric) {
    const sorted = line.words.sort((a, b) => a.bbox.x0 - b.bbox.x0);
    let segment = [];
    for (const word of sorted) {
      const previous = segment[segment.length - 1];
      if (previous) {
        const gap = word.bbox.x0 - previous.bbox.x1;
        const height = Math.max(
          previous.bbox.y1 - previous.bbox.y0,
          word.bbox.y1 - word.bbox.y0
        );
        if (gap > height * 6) {
          const completed = lineFromWords(segment);
          if (completed) lines.push(completed);
          segment = [];
        }
      }
      segment.push(word);
    }
    const completed = lineFromWords(segment);
    if (completed) lines.push(completed);
  }

  return lines.sort((a, b) => a.y0 - b.y0 || a.x0 - b.x0);
}

/**
 * Draw recognized lines invisibly onto a pdf-lib page. A line is one PDF text
 * object, allowing selection and copy/paste to continue across word spaces.
 */
export function embedWordsOnPdfPage(page, words, widthPx, font) {
  const scale = page.getWidth() / widthPx;
  const pageH = page.getHeight();
  const pdfLib = window.PDFLib || {};
  let embedded = 0;

  for (const line of groupWordsIntoLines(words)) {
    const text = line.words.map((word) => (word.text || "").trim()).filter(Boolean).join(" ");
    if (!text) continue;

    const x = line.x0 * scale;
    const heights = line.words
      .map((word) => word.bbox.y1 - word.bbox.y0)
      .sort((a, b) => a - b);
    const medianHeight = heights[Math.floor(heights.length / 2)];
    const size = Math.max(4, medianHeight * scale * 0.85);
    const targetWidth = Math.max(1, (line.x1 - line.x0) * scale);
    const naturalWidth = font.widthOfTextAtSize(text, size);
    const horizontalScale = naturalWidth > 0 ? targetWidth / naturalWidth : 1;
    const drawOptions = {
      x,
      y: pageH - line.y1 * scale + size * 0.04,
      size, font,
      // Near-zero opacity stays visually indistinguishable from the scan while
      // preserving an editable and selectable text run in ONLYOFFICE.
      opacity: 0.001,
    };

    try {
      const canScaleText = Math.abs(horizontalScale - 1) > 0.02
        && typeof page.pushOperators === "function"
        && typeof pdfLib.pushGraphicsState === "function"
        && typeof pdfLib.popGraphicsState === "function"
        && typeof pdfLib.concatTransformationMatrix === "function";

      if (canScaleText) {
        page.pushOperators(
          pdfLib.pushGraphicsState(),
          pdfLib.concatTransformationMatrix(horizontalScale, 0, 0, 1, x * (1 - horizontalScale), 0)
        );
        page.drawText(text, drawOptions);
        page.pushOperators(pdfLib.popGraphicsState());
      } else {
        page.drawText(text, drawOptions);
      }
      embedded += line.words.length;
    } catch { /* glyphs outside WinAnsi - skip the line */ }
  }
  return embedded;
}

// Prompt for a page range and recognize it. Returns null (user cancelled /
// unavailable) or [{ nPage, text, words, widthPx, heightPx }].
async function recognizePages(purposeLine) {
  if (!deps.isDocOpen()) return null;
  const { setStatus } = deps;

  if (!(await ocrAvailable())) {
    setStatus(`OCR-Daten fehlen in diesem Build (vendor/ocr - siehe "npm run fetch-ocr").`);
    return null;
  }

  const editor = deps.getEditor();
  const pageCount = editor.getCountPages() | 0;
  const spec = await deps.showPromptDialog(
    `${purposeLine}\nWelche Seiten? z.B. "1-3" - Dokument hat ${pageCount} Seite(n).`,
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
  setStatus("OCR-Modell wird geladen ...");
  const worker = await ensureWorker();

  const results = [];
  for (let i = 0; i < indexes.length; i++) {
    const nPage = indexes[i];
    setStatus(`Texterkennung: Seite ${nPage + 1} (${i + 1}/${indexes.length}) ...`);
    const widthPx = Math.round(doc.GetPageWidthMM(nPage) / 25.4 * OCR_DPI);
    const heightPx = Math.round(doc.GetPageHeightMM(nPage) / 25.4 * OCR_DPI);
    const canvas = r.GetPrintPage(nPage, widthPx, heightPx,
      window.AscPDF.PRINT_CONTENT_TYPES.docAndMarkups);
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

// tesseract.js v6 nests words under blocks/paragraphs/lines; older builds
// expose data.words directly. Preserve line identity in both cases.
function collectWords(data) {
  if (Array.isArray(data.words) && data.words.length) {
    return data.words.map((word) => ({ ...word, _ocrLine: wordLineKey(word) }));
  }
  const words = [];
  let lineIndex = 0;
  for (const block of data.blocks || []) {
    for (const para of block.paragraphs || []) {
      for (const line of para.lines || []) {
        for (const word of line.words || []) words.push({ ...word, _ocrLine: lineIndex });
        lineIndex++;
      }
    }
  }
  return words;
}

/** Embed recognized lines as an invisible, searchable text layer. */
export async function makeSearchablePdf() {
  const { setStatus } = deps;
  try {
    const results = await recognizePages("PDF durchsuchbar machen (OCR, Deutsch/Englisch):");
    if (!results) return;

    const totalWords = results.reduce((n, r) => n + r.words.length, 0);
    if (!totalWords) {
      setStatus("OCR abgeschlossen - kein Text erkannt, nichts einzubetten.");
      return;
    }

    setStatus("Unsichtbare Textebene wird eingebettet ...");
    const bytes = await deps.collectPdfBytes();
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
      embedded += embedWordsOnPdfPage(page, res.words, res.widthPx, font);
    }
    if (!embedded) {
      setStatus("OCR abgeschlossen - erkannte Wörter konnten nicht eingebettet werden.");
      return;
    }
    const outBytes = await pdf.save();
    deps.markClean();
    setStatus(`Durchsuchbare Textebene eingebettet: ${embedded} Wörter auf ${results.length} Seite(n) - bitte speichern.`);
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

    const text = results.map((r) => `-- Seite ${r.nPage + 1} --\n${r.text}`).join("\n\n");
    if (!text.replace(/-- Seite \d+ --/g, "").trim()) {
      setStatus("OCR abgeschlossen - kein Text erkannt.");
      return;
    }
    const outName = deps.getDocName().replace(/\.pdf$/i, "") + "-OCR.txt";
    downloadBytes(new TextEncoder().encode(text), outName, "text/plain;charset=utf-8");
    setStatus(`OCR abgeschlossen: "${outName}" (${results.length} Seite(n)).`);
  } catch (e) {
    console.error("OCR fehlgeschlagen:", e);
    setStatus(`OCR fehlgeschlagen: ${e.message}`);
  }
}
