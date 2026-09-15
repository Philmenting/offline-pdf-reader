import { downloadBytes } from "./dom.js";
import { IMAGE_PAGE_ACCEPT, isPageImage, imagePagePdf } from "./image-page.js";

// ponytail: sanitized copies are image-only. A vector redaction engine is a
// separate upgrade, never simulated by covering objects in the source PDF.
export function maskRegions(canvas, regions) {
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#000000";
  for (const r of regions) {
    if (![r.x, r.y, r.w, r.h].every(Number.isFinite) || r.x < 0 || r.y < 0 || r.w <= 0 || r.h <= 0 || r.x + r.w > 1.00001 || r.y + r.h > 1.00001) {
      throw new Error("Ungültiger Schwärzungsbereich.");
    }
    const left = Math.max(0, Math.floor(r.x * canvas.width) - 2);
    const top = Math.max(0, Math.floor(r.y * canvas.height) - 2);
    const right = Math.min(canvas.width, Math.ceil((r.x + r.w) * canvas.width) + 2);
    const bottom = Math.min(canvas.height, Math.ceil((r.y + r.h) * canvas.height) + 2);
    ctx.fillRect(left, top, right - left, bottom - top);
  }
}

export async function makeImageCopy(PDFLib, count, renderPage, options) {
  const pdf = await PDFLib.PDFDocument.create();
  for (let i = 0; i < count; i++) {
    options.check();
    options.progress(i + 1, count);
    const { canvas, size } = await renderPage(i, options.dpi);
    // Never mutate the SDK's cached page canvas.
    const image = document.createElement("canvas");
    image.width = canvas.width;
    image.height = canvas.height;
    const ctx = image.getContext("2d");
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, image.width, image.height);
    ctx.drawImage(canvas, 0, 0);
    maskRegions(image, options.regions?.get(i) || []);
    const blob = await new Promise((resolve, reject) => image.toBlob(
      b => b ? resolve(b) : reject(new Error("Seitenbild konnte nicht erzeugt werden.")),
      options.redact ? "image/png" : "image/jpeg", options.quality));
    const bytes = await blob.arrayBuffer();
    const embedded = options.redact ? await pdf.embedPng(bytes) : await pdf.embedJpg(bytes);
    pdf.addPage(size).drawImage(embedded, { x: 0, y: 0, width: size[0], height: size[1] });
    image.width = image.height = 1;
  }
  options.check();
  return pdf.save();
}

function dialog(markup) {
  const d = document.createElement("dialog");
  d.className = "document-dialog";
  d.innerHTML = markup;
  document.body.append(d);
  d.addEventListener("close", () => d.remove(), { once: true });
  d.showModal();
  return d;
}

export function wireDocumentTools(deps) {
  let busy = false;
  const MB = n => `${(n / 1048576).toFixed(2).replace(".", ",")} MB`;
  const saveCopy = async (bytes, name) => {
    if (window.desktop?.savePdf) {
      const result = await window.desktop.savePdf(bytes, name);
      if (result?.error) throw new Error(result.error);
      return !!result?.saved;
    }
    downloadBytes(bytes, name);
    return true;
  };

  async function importFiles(files) {
    if (busy || !files?.length || !deps.isOpen()) return;
    files = Array.from(files);
    if (files.some(f => !isPageImage(f) && !/\.pdf$/i.test(f.name) && f.type !== "application/pdf")) {
      deps.setStatus("Bitte nur PDF, PNG, JPEG oder WebP auswählen.");
      return;
    }
    busy = true;
    const token = deps.document();
    const count = deps.pageCount();
    const d = dialog(`<h2>Dateien einfügen</h2><ol class="import-files"></ol>
      <label>Einfügen <select id="import-position"><option value="end">Am Ende</option>
      <option value="before">Vor der aktuellen Seite</option><option value="after">Nach der aktuellen Seite</option>
      <option value="start">Am Anfang</option></select></label>
      <p role="status"></p><div class="dialog-actions"><button type="button" data-cancel>Abbrechen</button>
      <button type="button" id="import-confirm">Einfügen</button></div>`);
    const message = d.querySelector('[role="status"]');
    files.forEach(file => { const li = document.createElement("li"); li.textContent = file.name; d.querySelector("ol").append(li); });
    let canceled = false;
    const check = () => {
      if (canceled || deps.document() !== token || deps.pageCount() !== count) throw new Error("Vorgang abgebrochen.");
    };
    d.addEventListener("close", () => { canceled = true; busy = false; deps.refocus(); });
    d.querySelector("[data-cancel]").onclick = () => d.close();
    d.querySelector("#import-confirm").onclick = async event => {
      event.target.disabled = true;
      try {
        const position = d.querySelector("select").value;
        const index = position === "start" ? 0 : position === "end" ? count
          : deps.currentPage() + (position === "after" ? 1 : 0);
        await deps.loadPdfLib();
        const prepared = [];
        for (let i = 0; i < files.length; i++) {
          check();
          const file = files[i];
          message.textContent = `Datei ${i + 1} von ${files.length}: ${file.name}`;
          const bytes = isPageImage(file)
            ? await imagePagePdf(file, window.PDFLib, deps.pageSize(deps.currentPage()))
            : await file.arrayBuffer();
          const source = await window.PDFLib.PDFDocument.load(bytes);
          if (!source.getPageCount()) throw new Error("Die PDF enthält keine Seiten.");
          prepared.push({ bytes: new Uint8Array(bytes), count: source.getPageCount() });
        }
        check();
        await deps.insertPages(prepared, index, check);
        deps.setStatus(`${files.length} Dateien eingefügt. Noch nicht gespeichert.`);
        d.close();
      } catch (error) {
        message.textContent = error.message;
        event.target.disabled = false;
      }
    };
  }

  function chooseFiles() {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.accept = `application/pdf,.pdf,${IMAGE_PAGE_ACCEPT}`;
    input.onchange = () => importFiles(input.files);
    input.click();
  }

  async function exportCopy(redact) {
    if (busy || !deps.isOpen()) return;
    busy = true;
    deps.document().BlurActiveObject();
    const token = deps.document();
    const count = deps.pageCount();
    const name = deps.name().replace(/\.pdf$/i, "") + (redact ? "-geschwaerzt.pdf" : "-kompakt.pdf");
    let canceled = false;
    const regions = new Map();
    let preview = null;
    let pageIndex = deps.currentPage();
    const d = dialog(`<h2>${redact ? "Bereiche schwärzen" : "PDF verkleinern"}</h2>
      <p class="copy-warning">${redact ? "Die Kopie enthält nur bereinigte Seitenbilder. Verdeckte Inhalte, Anhänge und Metadaten werden nicht übernommen."
        : "Die kompakte Kopie enthält nur Seitenbilder."} Textsuche, interaktive Formulare und digitale Signaturen bleiben nicht erhalten. Das geöffnete Original bleibt unverändert.</p>
      <div class="copy-controls"><label>Seite <input id="copy-page" type="number" min="1" max="${count}" value="${pageIndex + 1}"></label>
      ${redact ? '<button id="clear-regions" type="button">Bereiche dieser Seite zurücksetzen</button>'
        : '<label>Qualität <select id="copy-quality"><option value="150">Gut, 150 dpi</option><option value="96">Klein, 96 dpi</option><option value="200">Hoch, 200 dpi</option></select></label>'}</div>
      ${redact ? '<p>Bereiche im Seitenbild aufziehen oder die Koordinaten in Prozent eingeben.</p><div class="region-controls">'
        + ['X', 'Y', 'Breite', 'Höhe'].map((label, i) => `<label>${label}<input id="region-${i}" type="number" min="0" max="100" value="${i < 2 ? 0 : 10}" step="0.1"></label>`).join('')
        + '<button id="add-region" type="button">Bereich hinzufügen</button></div>' : ''}
      <div class="copy-preview"><canvas id="copy-canvas" aria-label="Seitenvorschau"></canvas></div>
      <p id="copy-status" role="status"></p><div class="dialog-actions"><button type="button" data-cancel>Abbrechen</button>
      <button type="button" id="copy-create">${redact ? "Geschwärzte Kopie erzeugen" : "Größe berechnen"}</button>
      <button type="button" id="copy-save" disabled>Kopie speichern</button>
      ${!redact && window.desktop?.sendPdfByEmail ? '<button type="button" id="copy-mail" disabled>Per Mail</button>' : ''}</div>`);
    const status = d.querySelector("#copy-status");
    const canvas = d.querySelector("canvas");
    const create = d.querySelector("#copy-create");
    const save = d.querySelector("#copy-save");
    let result = null;
    const check = () => {
      if (canceled || deps.document() !== token || deps.pageCount() !== count) throw new Error("Vorgang abgebrochen.");
    };
    const invalidate = () => { result = null; save.disabled = true; if (d.querySelector("#copy-mail")) d.querySelector("#copy-mail").disabled = true; };
    const paint = () => {
      if (!preview) return;
      canvas.width = preview.width;
      canvas.height = preview.height;
      canvas.getContext("2d").drawImage(preview, 0, 0);
      maskRegions(canvas, regions.get(pageIndex) || []);
    };
    const showPage = async () => {
      const value = Number(d.querySelector("#copy-page").value);
      if (!Number.isInteger(value) || value < 1 || value > count) return;
      pageIndex = value - 1;
      const requested = pageIndex;
      preview = null;
      canvas.style.pointerEvents = "none";
      try {
        const rendered = await deps.renderPage(requested, redact ? 100 : Number(d.querySelector("#copy-quality").value));
        check();
        if (pageIndex !== requested) return;
        if (redact) preview = rendered.canvas;
        else {
          const blob = await new Promise(resolve => rendered.canvas.toBlob(resolve, "image/jpeg", .78));
          if (!blob) throw new Error("Vorschau konnte nicht erzeugt werden.");
          const bitmap = await createImageBitmap(blob);
          if (canceled || pageIndex !== requested) { bitmap.close(); return; }
          const copy = document.createElement("canvas"); copy.width = bitmap.width; copy.height = bitmap.height;
          copy.getContext("2d").drawImage(bitmap, 0, 0); bitmap.close();
          preview = copy;
        }
        paint();
        canvas.style.pointerEvents = "";
      } catch (error) { status.textContent = error.message; }
    };
    const addRegion = r => {
      if (![r.x, r.y, r.w, r.h].every(Number.isFinite) || r.x < 0 || r.y < 0 || r.w <= 0 || r.h <= 0 || r.x + r.w > 1.00001 || r.y + r.h > 1.00001) {
        status.textContent = "Der Bereich muss vollständig innerhalb der Seite liegen.";
        return;
      }
      if (!regions.has(pageIndex)) regions.set(pageIndex, []);
      regions.get(pageIndex).push(r);
      invalidate(); paint();
      status.textContent = `${[...regions.values()].reduce((sum, rows) => sum + rows.length, 0)} Bereiche vorgemerkt.`;
    };
    if (redact) {
      canvas.style.touchAction = "none";
      let start = null;
      const point = e => {
        const b = canvas.getBoundingClientRect();
        return { x: Math.max(0, Math.min(1, (e.clientX - b.left) / b.width)), y: Math.max(0, Math.min(1, (e.clientY - b.top) / b.height)) };
      };
      canvas.onpointerdown = e => { if (e.button !== 0) return; start = point(e); canvas.setPointerCapture(e.pointerId); };
      canvas.onpointermove = e => {
        if (!start) return;
        const end = point(e); paint();
        maskRegions(canvas, [{ x: Math.min(start.x, end.x), y: Math.min(start.y, end.y), w: Math.max(.00001, Math.abs(start.x - end.x)), h: Math.max(.00001, Math.abs(start.y - end.y)) }]);
      };
      canvas.onpointercancel = () => { start = null; paint(); };
      canvas.onpointerup = e => {
        if (!start) return;
        const end = point(e);
        const r = { x: Math.min(start.x, end.x), y: Math.min(start.y, end.y), w: Math.abs(start.x - end.x), h: Math.abs(start.y - end.y) };
        start = null; paint();
        if (r.w > .001 && r.h > .001) addRegion(r);
      };
      d.querySelector("#add-region").onclick = () => {
        const [x, y, w, h] = [0, 1, 2, 3].map(i => Number(d.querySelector(`#region-${i}`).value) / 100);
        addRegion({ x, y, w, h });
      };
      d.querySelector("#clear-regions").onclick = () => { regions.delete(pageIndex); invalidate(); paint(); };
    } else d.querySelector("#copy-quality").onchange = () => { invalidate(); showPage(); };
    d.querySelector("#copy-page").onchange = showPage;
    d.querySelector("[data-cancel]").onclick = () => d.close();
    d.addEventListener("close", () => { canceled = true; busy = false; result = null; deps.refocus(); });
    create.onclick = async () => {
      if (redact && ![...regions.values()].some(r => r.length)) { status.textContent = "Bitte zuerst einen Bereich markieren."; return; }
      invalidate();
      const controls = [...d.querySelectorAll("input, select, button:not([data-cancel])")];
      controls.forEach(c => c.disabled = true);
      canvas.style.pointerEvents = "none";
      try {
        await deps.loadPdfLib();
        result = await makeImageCopy(window.PDFLib, count, deps.renderPage, {
          redact, regions, dpi: redact ? 200 : Number(d.querySelector("#copy-quality").value), quality: .78,
          check, progress: (page, total) => status.textContent = `Seite ${page} von ${total} wird verarbeitet …`,
        });
        check();
        const original = redact ? null : await deps.collectPdfBytes();
        check();
        status.textContent = `Kopie: ${MB(result.length)}${original ? `, bisher: ${MB(original.length)}${result.length >= original.length ? ". Keine Verkleinerung erreicht." : ""}` : ". Bitte die geschwärzten Bereiche vor der Weitergabe prüfen."}`;
      } catch (error) { invalidate(); status.textContent = error.message; }
      finally {
        controls.forEach(c => c.disabled = false);
        save.disabled = !result;
        if (d.querySelector("#copy-mail")) d.querySelector("#copy-mail").disabled = !result;
        canvas.style.pointerEvents = "";
      }
    };
    save.onclick = async () => {
      try { check(); if (result && await saveCopy(result, name)) { deps.setStatus(`Kopie gespeichert: ${name}`); d.close(); } }
      catch (error) { status.textContent = error.message; }
    };
    const mail = d.querySelector("#copy-mail");
    if (mail) mail.onclick = async () => {
      try {
        check();
        if (!result) return;
        const res = await window.desktop.sendPdfByEmail(result, name);
        status.textContent = res?.error || (res?.ok ? "Mailentwurf vorbereitet." : "Mailversand abgebrochen.");
      } catch (error) { status.textContent = error.message; }
    };
    await showPage();
  }

  return { chooseFiles, importFiles, redact: () => exportCopy(true), compress: () => exportCopy(false), isBusy: () => busy };
}
