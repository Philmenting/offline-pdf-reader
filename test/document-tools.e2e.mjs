import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { PDFDocument, StandardFonts, PDFName } from "pdf-lib";
import { chromium } from "playwright";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const port = process.env.TOOLS_TEST_PORT || "4001";
const base = `http://127.0.0.1:${port}`;
const server = spawn(process.execPath, ["server.mjs"], { cwd: root, env: { ...process.env, PORT: port }, stdio: "pipe" });
let browser;
async function fixture(text, pages = 1) {
  const pdf = await PDFDocument.create();
  pdf.setTitle("PRIVATE METADATA");
  await pdf.attach(new TextEncoder().encode("PRIVATE ATTACHMENT"), "private.txt");
  const font = await pdf.embedFont(StandardFonts.Helvetica);
  for (let i = 0; i < pages; i++) pdf.addPage([400, 500]).drawText(text, { x: 50, y: 400, size: 22, font });
  return Buffer.from(await pdf.save());
}
async function tool(page, name) {
  const button = page.locator(`[data-tool="${name}"]`);
  await button.evaluate(b => { const menu = b.closest("details"); if (menu) menu.open = true; });
  await button.click();
}
async function open(page, bytes, name = "test.pdf") {
  await page.goto(base);
  await page.waitForFunction(() => window.__pdfEditorReady, null, { timeout: 90000 });
  await page.locator("#file-input").setInputFiles({ name, mimeType: "application/pdf", buffer: bytes });
  await page.waitForFunction(() => document.querySelector("#status").textContent.includes("bereit zum Bearbeiten"), null, { timeout: 90000 });
}
async function downloaded(page, action) {
  const pending = page.waitForEvent("download", { timeout: 90000 });
  await action();
  const download = await pending;
  const chunks = [];
  for await (const chunk of await download.createReadStream()) chunks.push(chunk);
  return Buffer.concat(chunks);
}

try {
  for (let i = 0; ; i++) {
    try { if ((await fetch(base)).ok) break; } catch {}
    if (i === 150) throw new Error("Testserver konnte nicht gestartet werden.");
    await new Promise(r => setTimeout(r, 100));
  }
  assert.equal((await fetch(`${base}/%ZZ`)).status, 400);
  assert.equal((await fetch(`${base}/%5Cwindows`)).status, 400);
  browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  page.on("dialog", d => d.accept());
  const source = await fixture("SECRET-123", 2);
  const incomingForm = await PDFDocument.load(await fixture("INCOMING"));
  const field = incomingForm.getForm().createTextField("imported-field");
  field.setText("Preserved value");
  field.addToPage(incomingForm.getPage(0), { x: 20, y: 20, width: 160, height: 25 });
  await open(page, source);
  await tool(page, "edit-text");
  await page.waitForFunction(() => window.__pdfEditor.getDocumentRenderer().file.pages[0].isRecognized, null, { timeout: 90000 });
  await page.waitForFunction(() => window.__pdfEditor.getPDFDoc().GetPageInfo(0).drawings.some(d =>
    d.GetDocContent?.()?.GetAllParagraphs({ All: true }).some(p => p.IsRecalculated())), null, { timeout: 30000 });
  const exact = await page.evaluate(async () => {
    const { editableTextLines } = await import("/js/modules/editable-text.js");
    const doc = window.__pdfEditor.getPDFDoc();
    const result = editableTextLines(doc, 0, 1000);
    return { result, layout: doc.GetPageInfo(0).drawings.map(d => ({
      shape: d.IsShape?.(), transform: d.transformText,
      lines: d.GetDocContent?.()?.GetAllParagraphs({ All: true }).map(p => ({ lines: p.Lines, pages: p.Pages, ready: p.IsRecalculated() })),
    })) };
  });
  assert.ok(exact.result?.some(w => w.text.includes("SECRET-123")), JSON.stringify(exact));
  await tool(page, "select");
  console.log("PASS known text and SDK line positions can be exported without OCR");

  // Mixed import is one transaction and inserts before page 1 in file order.
  const png = Buffer.from(await page.evaluate(() => {
    const c = document.createElement("canvas"); c.width = 120; c.height = 80;
    c.getContext("2d").fillRect(0, 0, 120, 80);
    return c.toDataURL("image/png").split(",")[1];
  }), "base64");
  const chooser = page.waitForEvent("filechooser");
  await tool(page, "files-insert");
  await (await chooser).setFiles([
    { name: "incoming.pdf", mimeType: "application/pdf", buffer: Buffer.from(await incomingForm.save()) },
    { name: "image.png", mimeType: "image/png", buffer: png },
  ]);
  await page.selectOption("#import-position", "before");
  await page.click("#import-confirm");
  await page.waitForFunction(() => window.__pdfEditor.getCountPages() === 4);
  await page.waitForSelector(".document-dialog", { state: "detached" });
  await page.waitForFunction(() => window.__pdfEditor.getPDFDoc().widgets.some(w => w.GetValue?.() === "Preserved value"), null, { timeout: 30000 });
  assert.equal(await page.evaluate(() => window.__pdfEditor.getPDFDoc().Viewer.file.getPageWidth(0)), 400);
  const extractedForm = await downloaded(page, async () => {
    await tool(page, "pdf-extract");
    await page.fill("#prompt-input", "1");
    await page.click("#prompt-ok");
  });
  const extracted = await PDFDocument.load(extractedForm);
  assert.equal(extracted.getPageCount(), 1);
  const exportedFields = extracted.getForm().getFields().map(f => ({ name: f.getName(), text: f.getText?.() }));
  assert.ok(exportedFields.some(f => f.text === "Preserved value"), JSON.stringify(exportedFields));
  console.log("PASS extracting a page preserves its filled form field");
  await tool(page, "undo");
  await page.waitForFunction(() => window.__pdfEditor.getCountPages() === 2);
  console.log("PASS mixed import at chosen position is undoable");

  // A malformed second input cannot leave the first input partially inserted.
  const chooser2 = page.waitForEvent("filechooser");
  await tool(page, "files-insert");
  await (await chooser2).setFiles([
    { name: "ok.pdf", mimeType: "application/pdf", buffer: source },
    { name: "bad.pdf", mimeType: "application/pdf", buffer: Buffer.from("broken") },
  ]);
  await page.click("#import-confirm");
  await page.waitForFunction(() => !document.querySelector("#import-confirm").disabled);
  assert.equal(await page.evaluate(() => window.__pdfEditor.getCountPages()), 2);
  await page.locator(".document-dialog [data-cancel]").click();
  console.log("PASS malformed batch does not partially modify the document");

  await tool(page, "redact-copy");
  await page.waitForFunction(() => document.querySelector("#copy-canvas").width > 300);
  // Entire first page is redacted for an unambiguous pixel/content check.
  await page.fill("#region-2", "100");
  await page.fill("#region-3", "100");
  await page.click("#add-region");
  await page.click("#copy-create");
  await page.waitForFunction(() => !document.querySelector("#copy-save").disabled, null, { timeout: 90000 });
  await page.screenshot({ path: join(root, "redaction-preview.png") });
  const redacted = await downloaded(page, () => page.click("#copy-save"));
  const pdf = await PDFDocument.load(redacted);
  assert.equal(pdf.getPageCount(), 2);
  assert.equal(pdf.getTitle(), undefined);
  assert.equal(pdf.catalog.has(PDFName.of("Names")), false);
  assert.equal(pdf.catalog.has(PDFName.of("AcroForm")), false);
  for (const p of pdf.getPages()) {
    const annots = p.node.lookup(PDFName.of("Annots"));
    assert.ok(!annots || annots.size() === 0);
    const fonts = p.node.Resources()?.lookup(PDFName.of("Font"));
    assert.ok(!fonts || fonts.keys().length === 0);
  }
  assert.equal(await page.evaluate(() => window.__pdfEditor.getCountPages()), 2);
  console.log("PASS redaction creates an image-only copy without original metadata, text fonts, forms or attachments");

  await open(page, redacted, "redacted.pdf");
  const redactionPixels = await page.evaluate(async () => {
    const view = window.__pdfEditor.getDocumentRenderer();
    let image;
    for (let i = 0; i < 100 && !image; i++) { image = view.file.getPage(0, 400, 500); if (!image) await new Promise(r => setTimeout(r, 100)); }
    const canvas = document.createElement("canvas"); canvas.width = 400; canvas.height = 500;
    canvas.getContext("2d").drawImage(image, 0, 0);
    return Array.from(canvas.getContext("2d").getImageData(200, 200, 1, 1).data);
  });
  assert.ok(redactionPixels.slice(0, 3).every(c => c < 5));
  console.log("PASS redacted area stays black after reopening");

  await tool(page, "compress-copy");
  await page.selectOption("#copy-quality", "96");
  await page.click("#copy-create");
  await page.waitForFunction(() => !document.querySelector("#copy-save").disabled, null, { timeout: 90000 });
  assert.match(await page.locator("#copy-status").textContent(), /bisher:/);
  const compact = await downloaded(page, () => page.click("#copy-save"));
  assert.equal((await PDFDocument.load(compact)).getPageCount(), 2);
  console.log("PASS compact copy includes size comparison and valid pages");

  // Restore keeps the only crash snapshot until an explicit successful save.
  await page.evaluate(async bytes => {
    const storage = await import("/js/modules/storage.js");
    await storage.recoverySave(new Uint8Array(bytes), "recovered.pdf");
  }, Array.from(source));
  await page.reload();
  await page.waitForFunction(() => document.title.includes("recovered.pdf") && document.title.startsWith("•"), null, { timeout: 90000 });
  assert.ok(await page.evaluate(async () => !!(await (await import("/js/modules/storage.js")).recoveryLoad())));
  await downloaded(page, () => page.click("#btn-save"));
  await page.waitForFunction(async () => !(await (await import("/js/modules/storage.js")).recoveryLoad()));
  console.log("PASS recovered document stays unsaved and retains backup until save");
  assert.deepEqual(errors, []);
  await page.close();
} finally {
  if (browser) await browser.close();
  server.kill();
}
