#!/usr/bin/env node
/**
 * End-to-end smoke test for the PDF editor's typed-text pipeline.
 *
 * Guards the font stack against the regression class that produced ".notdef
 * box" / disappearing-character artifacts when typing into PDF text:
 *   1. empty g_fonts_selection_bin → every font name resolved to the ASCW3
 *      dummy mini-font,
 *   2. odttf XOR corruption of served fonts → no non-embedded font could be
 *      opened, typed characters collapsed onto the embedded subset font's
 *      empty .notdef glyph (all sharing ONE grapheme).
 *
 * Flow: build a test PDF with embedded SUBSET fonts (Chromium printToPDF),
 * serve the repo, open the editor, enter page-text edit mode, type text that
 * contains characters NOT present in the subset, then assert:
 *   • the selection table is loaded (list > 100 records),
 *   • standard names resolve to their metric equivalents, never to ASCW3,
 *   • every typed character got a real, per-character grapheme (the artifact
 *     bug collapsed distinct characters onto one shared .notdef grapheme),
 *   • no page errors.
 *
 * Prerequisites: npm run build-engine && npm run generate-fonts.
 * Browser: uses `playwright` (CI: npx playwright install chromium) or
 * `playwright-core` with CHROMIUM_PATH pointing at a Chromium binary.
 *
 * Usage: node test/e2e-smoke.mjs
 */
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const PORT = process.env.SMOKE_PORT || "3999";
const BASE = `http://127.0.0.1:${PORT}`;

let failures = 0;
function check(name, ok, detail) {
  const mark = ok ? "✓" : "✗";
  console.log(`  ${mark} ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures++;
}

// main.js's showPromptDialog() replaces window.prompt() (unsupported in
// Electron's BrowserWindow) with an in-page modal — drive it like a normal
// form control instead of Playwright's native page.on("dialog").
async function fillPromptDialog(page, value) {
  await page.waitForSelector("#prompt-dialog:not([hidden])", { timeout: 10000 });
  await page.fill("#prompt-input", value);
  await page.click("#prompt-ok");
}

// Click coordinates below are calibrated for a 52px-high header at 100% zoom
// in a 1280px viewport. The toolbar may wrap to more rows (taller header), so
// shift all page-area Y coordinates by the actual header growth.
async function headerYOffset(page) {
  const h = await page.evaluate(() => document.querySelector(".app-header").getBoundingClientRect().height);
  return Math.round(h - 52);
}

// Some tools live inside the toolbar's "Seiten"/"Extras" dropdown menus
// (<details class="menu">). Their buttons are unclickable while the menu is
// closed, so open the owning menu first when there is one.
async function clickTool(page, tool) {
  const sel = `[data-tool="${tool}"]`;
  const menuId = await page.evaluate((s) => {
    const btn = document.querySelector(s);
    const menu = btn && btn.closest("details.menu");
    if (menu) menu.open = true;
    return menu ? menu.id : null;
  }, sel);
  await page.click(sel);
  if (menuId) await page.evaluate((id) => { document.getElementById(id).open = false; }, menuId);
}

async function launchChromium() {
  let chromium;
  try { ({ chromium } = await import("playwright")); }
  catch { ({ chromium } = await import("playwright-core")); }
  const opts = {};
  if (process.env.CHROMIUM_PATH) opts.executablePath = process.env.CHROMIUM_PATH;
  return chromium.launch(opts);
}

async function waitForServer(url, timeoutMs = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(url);
      if (res.ok) return;
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`server at ${url} did not come up`);
}

// Build a single-page AcroForm PDF (text field "name" + checkbox
// "einverstanden") with known geometry, so screen coordinates are stable at
// 100% zoom in a 1280x900 viewport.
async function makeFormPdf() {
  const { PDFDocument, StandardFonts } = await import("pdf-lib");
  const doc = await PDFDocument.create();
  const page = doc.addPage([595, 842]);
  const font = await doc.embedFont(StandardFonts.Helvetica);
  page.drawText("Antragsformular", { x: 50, y: 780, size: 24, font });
  page.drawText("Name:", { x: 50, y: 720, size: 12, font });
  page.drawText("Einverstanden:", { x: 50, y: 670, size: 12, font });
  const form = doc.getForm();
  form.createTextField("name").addToPage(page, { x: 150, y: 705, width: 250, height: 24 });
  form.createCheckBox("einverstanden").addToPage(page, { x: 150, y: 660, width: 18, height: 18 });
  return Buffer.from(await doc.save());
}

async function testFormRoundtrip(browser) {
  const formPdf = await makeFormPdf();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));
  // reopening with unsaved changes asks for confirmation — accept it
  page.on("dialog", (d) => d.accept());

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  const openBytes = async (buffer, name) => {
    await (await page.$("#file-input")).setInputFiles({ name, mimeType: "application/pdf", buffer });
    // opening a second document reloads the page (fresh editor); the wait
    // must survive that navigation
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        await page.waitForFunction(
          () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
          null, { timeout: 90000 });
        break;
      } catch (e) {
        if (!/Execution context was destroyed|navigat/i.test(String(e))) throw e;
      }
    }
    await page.waitForTimeout(2000);
  };
  await openBytes(formPdf, "form.pdf");
  console.log("form document open");

  // fill mode: text field at PDF pts (150..400, 705..729), checkbox at
  // (150..168, 660..678) → screen at 100% zoom (page top-left ~336/72,
  // scale 96/72): field center ~(703,238), checkbox ~(548,302)
  await clickTool(page, "form-fill");
  await page.waitForTimeout(800);
  const dy = await headerYOffset(page);
  await page.mouse.click(703, 238 + dy);
  await page.waitForTimeout(1200);
  const NAME = "Philipp Holzwarth";
  await page.keyboard.type(NAME, { delay: 60 });
  await page.waitForTimeout(600);
  await page.mouse.click(548, 302 + dy); // checkbox (commits the text field)
  await page.waitForTimeout(1000);
  await page.mouse.click(950, 500 + dy); // blur
  await page.waitForTimeout(800);

  const readValues = () => page.evaluate(() => {
    const doc = window.__pdfEditor.getPDFDoc();
    const out = {};
    for (const f of doc.widgets) {
      out[f.GetFullName()] = f.IsChecked ? { v: f.GetValue(), c: f.IsChecked() } : { v: f.GetValue() };
    }
    return out;
  });

  const filled = await readValues();
  check("text field holds the full typed value (no dropped first key)",
    filled.name && filled.name.v === NAME, JSON.stringify(filled.name));
  check("checkbox toggled on", !!(filled.einverstanden && filled.einverstanden.c),
    JSON.stringify(filled.einverstanden));

  // save through the real serializer and reopen the bytes
  const saved = await page.evaluate(() => {
    const e = window.__pdfEditor;
    const doc = e.getPDFDoc();
    const n = e.getCountPages() | 0;
    const bytes = doc.GetPagesBinary(Array.from({ length: n }, (_, i) => i), false);
    return bytes ? Array.from(bytes) : null;
  });
  check("form save produces a real PDF",
    !!saved && String.fromCharCode(...saved.slice(0, 5)) === "%PDF-" && saved.length > 1000,
    saved ? `${saved.length} bytes` : "no bytes");

  await openBytes(Buffer.from(saved), "form-saved.pdf");
  const reopened = await readValues();
  check("reopened PDF keeps the text value",
    reopened.name && reopened.name.v === NAME, JSON.stringify(reopened.name));
  check("reopened PDF keeps the checkbox state",
    !!(reopened.einverstanden && reopened.einverstanden.c), JSON.stringify(reopened.einverstanden));

  check("no page errors (form scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

// Build a small multi-page PDF to append via the ＋PDF button.
async function makeMultiPagePdf(count) {
  const { PDFDocument, StandardFonts } = await import("pdf-lib");
  const doc = await PDFDocument.create();
  const font = await doc.embedFont(StandardFonts.Helvetica);
  for (let i = 1; i <= count; i++) {
    const page = doc.addPage([595, 842]);
    page.drawText(`SEITE ${i}`, { x: 50, y: 780, size: 40, font });
  }
  return Buffer.from(await doc.save());
}

// Build a one-page PDF with an embedded raster image, to exercise
// CPDFDoc.EditPage()'s inline data:-URI picture path.
async function makeImagePdf() {
  const { PDFDocument, StandardFonts } = await import("pdf-lib");
  const doc = await PDFDocument.create();
  const font = await doc.embedFont(StandardFonts.Helvetica);
  const pngB64 = "iVBORw0KGgoAAAANSUhEUgAAABQAAAAUCAYAAACNiR0NAAAAF0lEQVR42mNk+M9QzwAEjIiIQQwYo6EBAKr9BAvXBGvzAAAAAElFTkSuQmCC";
  const png = await doc.embedPng(Buffer.from(pngB64, "base64"));
  const page = doc.addPage([595, 842]);
  page.drawText("Seite mit eingebettetem Bild", { x: 50, y: 780, size: 20, font });
  page.drawImage(png, { x: 450, y: 750, width: 80, height: 80 });
  return Buffer.from(await doc.save());
}

// Regression test for a missing-logo bug: CPDFDoc.EditPage() (entered via
// "Text" mode) routes every inline data:-URI picture on the page through
// AscCommon.sendImgUrls, expecting a callback with resolved URLs before the
// picture gets registered with the image loader that actually paints it.
// The stock implementations target either a collaborative Document Server
// or the native desktop shell's window.AscDesktopEditor — neither exists in
// this offline Electron/browser app, so the callback never fired and the
// picture rendered as a permanent placeholder box showing only its shape
// name (reported by a real user as their government-document letterhead
// logo vanishing). main.js patches AscCommon.sendImgUrls to answer
// synchronously since these images are already self-contained data: URIs.
async function testEditPageImageRendering(browser) {
  const imagePdf = await makeImagePdf();

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "image.pdf", mimeType: "application/pdf", buffer: imagePdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1200);
  console.log("edit-page-image document open");

  await clickTool(page, "edit-text");
  await page.waitForTimeout(800);

  const info = await page.evaluate(() => {
    const doc = window.__pdfEditor.getPDFDoc();
    const list = doc.Viewer.pagesInfo.pages[0].drawings;
    const imgDrawing = list.find((d) => d.constructor && d.constructor.name === "CImageShape");
    if (!imgDrawing) return { found: false };
    const url = imgDrawing.getBlipFill().RasterImageId;
    const entry = window.AscCommon.g_image_loader.map_image_index[url];
    return { found: true, registered: !!entry, status: entry ? entry.Status : null };
  });
  check("entering text-edit mode finds the page's picture drawing", info.found, JSON.stringify(info));
  check("the picture's image gets registered with the loader that paints it (not stuck as a name-only placeholder)",
    info.registered && info.status === 1, JSON.stringify(info));

  check("no page errors (edit-page-image scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

// "Wasserzeichen": a Word-style diagonal text watermark, BAKED into the page
// content with pdf-lib (annotations can't rotate freely, drawings don't
// survive the split-based save at all). The flow serializes the current
// document, stamps every page, and reloads the editor with the new bytes —
// so the test must ride through a page reload, then verify grey watermark
// pixels actually render in the page centre.
async function testWatermark(browser) {
  const sourcePdf = await makeMultiPagePdf(3);

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "three.pdf", mimeType: "application/pdf", buffer: sourcePdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1200);
  console.log("watermark document open");

  await clickTool(page, "watermark");
  await page.waitForSelector("#prompt-dialog:not([hidden])", { timeout: 10000 });
  await page.fill("#prompt-input", "VERTRAULICH");
  await Promise.all([
    page.waitForNavigation({ waitUntil: "load", timeout: 90000 }), // the tool reloads with the stamped bytes
    page.click("#prompt-ok"),
  ]);
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1500);

  const state = await page.evaluate(() => {
    const file = window.__pdfEditor.getDocumentRenderer().file;
    const pages = window.__pdfEditor.getCountPages();
    // raw page render (no annotations): baked-in grey pixels must show up
    const canvas = file.getPage(0, 400, 566);
    const d = canvas.getContext("2d").getImageData(150, 233, 100, 100).data;
    let grey = 0;
    for (let i = 0; i < d.length; i += 4) {
      const r = d[i], g = d[i + 1], b = d[i + 2];
      if (r > 180 && r < 245 && Math.abs(r - g) < 12 && Math.abs(g - b) < 12) grey++;
    }
    return { pages, grey };
  });
  check("watermarked document keeps its page count", state.pages === 3, `pages=${state.pages}`);
  check("diagonal watermark is baked into the page content (grey pixels at page centre)",
    state.grey > 500, `greyPixels=${state.grey}`);

  check("no page errors (watermark scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

// Regression: opening a page in text-edit mode ("Text" → EditPage marks it
// isRecognized) then saving must NOT blank the page. Upstream's split-save
// emitted ctPageClear for recognized pages but can't re-serialize their
// drawings (no WASM support), erasing the page. patchSaveNoPageClear in
// build-onlyoffice-pdf.mjs keeps the original content instead.
async function testTextModeSaveKeepsContent(browser) {
  const sourcePdf = await makeMultiPagePdf(2);

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "two.pdf", mimeType: "application/pdf", buffer: sourcePdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1200);
  console.log("text-mode-save document open");

  await clickTool(page, "edit-text");
  await page.waitForTimeout(1500);
  const recognized = await page.evaluate(() =>
    window.__pdfEditor.getPDFDoc().Viewer.file.pages[0].isRecognized);
  check("entering text mode recognizes the page", recognized === true, `isRecognized=${recognized}`);

  const savedB64 = await page.evaluate(() => {
    const doc = window.__pdfEditor.getPDFDoc();
    try { doc.BlurActiveObject(); } catch { /* no active object */ }
    const n = window.__pdfEditor.getCountPages() | 0;
    const idx = Array.from({ length: n }, (_, i) => i);
    const r = doc.GetPagesBinary(idx, false);
    const arr = new Uint8Array(r);
    let s = "";
    for (let i = 0; i < arr.length; i++) s += String.fromCharCode(arr[i]);
    return btoa(s);
  });
  const savedBytes = Buffer.from(savedB64, "base64");
  check("saving after text mode produces a real PDF", savedBytes.slice(0, 5).toString("latin1") === "%PDF-",
    `${savedBytes.length} bytes`);

  const page2 = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page2.on("pageerror", (e) => pageErrors.push(e.message));
  await page2.goto(BASE);
  await page2.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page2.$("#file-input")).setInputFiles({ name: "reopened.pdf", mimeType: "application/pdf", buffer: savedBytes });
  await page2.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page2.waitForTimeout(1200);

  const darkPixels = await page2.evaluate(() => {
    const file = window.__pdfEditor.getDocumentRenderer().file;
    const canvas = file.getPage(0, 400, 566);
    const d = canvas.getContext("2d").getImageData(0, 0, 400, 566).data;
    let dark = 0;
    for (let i = 0; i < d.length; i += 4) {
      if (d[i] < 100 && d[i + 1] < 100 && d[i + 2] < 100) dark++;
    }
    return dark;
  });
  check("the text-edited page is NOT blanked by saving (page text still renders)",
    darkPixels > 200, `darkPixels=${darkPixels}`);

  check("no page errors (text-mode-save scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
  await page2.close();
}

// "Seitenzahlen": same page-loop/annotation mechanism as the watermark, with
// per-page computed bottom-center rect and "N / total" content.
async function testPageNumbers(browser) {
  const sourcePdf = await makeMultiPagePdf(3);

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "three.pdf", mimeType: "application/pdf", buffer: sourcePdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1200);
  console.log("page-numbers document open");

  await clickTool(page, "page-numbers");
  await page.waitForTimeout(500);

  const contents = await page.evaluate(() => {
    const doc = window.__pdfEditor.getPDFDoc();
    const n = doc.GetPagesCount();
    const out = [];
    for (let i = 0; i < n; i++) {
      const annots = doc.GetPageInfo(i).annots;
      out.push(annots.length ? annots[annots.length - 1].GetContents() : null);
    }
    return out;
  });
  check("page numbers show the correct N / total on every page",
    JSON.stringify(contents) === JSON.stringify(["1 / 3", "2 / 3", "3 / 3"]), JSON.stringify(contents));

  check("no page errors (page-numbers scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

// "Bilder extrahieren": pull every embedded picture out as its own download,
// de-duplicating a logo repeated across pages, and force-recognizing
// not-yet-"Text"-edited pages first (their pictures don't show up in
// GetPageInfo().drawings until CPDFDoc.EditPage() has run for that page).
async function testExtractEmbeddedImages(browser) {
  const { PDFDocument } = await import("pdf-lib");
  const doc = await PDFDocument.create();
  const pngB64 = "iVBORw0KGgoAAAANSUhEUgAAABQAAAAUCAYAAACNiR0NAAAAF0lEQVR42mNk+M9QzwAEjIiIQQwYo6EBAKr9BAvXBGvzAAAAAElFTkSuQmCC";
  const png = await doc.embedPng(Buffer.from(pngB64, "base64"));
  const p1 = doc.addPage([300, 300]); p1.drawImage(png, { x: 50, y: 50, width: 100, height: 100 });
  const p2 = doc.addPage([300, 300]); p2.drawImage(png, { x: 50, y: 50, width: 100, height: 100 }); // same image again
  doc.addPage([300, 300]); // page with no image at all
  const bytes = Buffer.from(await doc.save());

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "images.pdf", mimeType: "application/pdf", buffer: bytes });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1200);
  console.log("extract-embedded-images document open");

  const downloads = [];
  page.on("download", (d) => downloads.push(d));
  await clickTool(page, "extract-images");
  await page.waitForTimeout(1500);
  check("extracting images de-duplicates the repeated logo (exactly 1 download)", downloads.length === 1, `count=${downloads.length}`);
  if (downloads.length) {
    const path = await downloads[0].path();
    const fileBytes = await (await import("node:fs/promises")).readFile(path);
    check("extracted image is a real PNG", fileBytes.slice(1, 4).toString("latin1") === "PNG", fileBytes.slice(0, 8).toString("hex"));
  }

  check("no page errors (extract-embedded-images scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

async function testExtractEmbeddedImagesNone(browser) {
  const sourcePdf = await makeMultiPagePdf(2);

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "noimg.pdf", mimeType: "application/pdf", buffer: sourcePdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1200);

  await clickTool(page, "extract-images");
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("Keine eingebetteten Bilder"),
    null, { timeout: 5000 }).then(
    () => check("no-images case shows a clear status message", true),
    () => check("no-images case shows a clear status message", false));

  check("no page errors (extract-embedded-images-none scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

// "Als Bilder": export a page range to standalone PNGs at a chosen DPI via
// the offscreen print-page renderer (no navigation to the page required).
async function testExportPagesAsImages(browser) {
  const sourcePdf = await makeMultiPagePdf(3);

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "three.pdf", mimeType: "application/pdf", buffer: sourcePdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1200);
  console.log("export-pages-as-images document open");

  const downloads = [];
  page.on("download", (d) => downloads.push(d));
  await clickTool(page, "pages-to-images");
  await fillPromptDialog(page, "1-2");   // page-range modal
  await fillPromptDialog(page, "100");   // DPI modal, kept low for test speed
  await page.waitForTimeout(3000);

  check("exporting a 2-page range produces 2 image downloads", downloads.length === 2, `count=${downloads.length}`);
  if (downloads.length) {
    const path = await downloads[0].path();
    const fileBytes = await (await import("node:fs/promises")).readFile(path);
    check("exported file is a real PNG", fileBytes.slice(1, 4).toString("latin1") === "PNG", fileBytes.slice(0, 8).toString("hex"));
  }

  check("no page errors (export-pages-as-images scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

// Regression test for a scrollbar-freeze bug: the thumbnail sidebar (a
// CDocument instance, aliased AscCommon.ThumbnailsControl) marks itself
// dirty via setNeedResize(true) after a page-count change, but only
// actually recomputes on the NEXT checkTasks() poll — and checkTasks()
// SKIPS that recompute entirely while pdfDoc.fontLoader.isWorking() is
// true. A merged PDF that pulls in fonts outside the bundled set can keep
// the loader "working" indefinitely, so the thumbnail list's scrollbar
// never catches up to the new page count. main.js's appendPdf() now calls
// forceViewerResync() right after the merge (and again after a delay) to
// bypass that gate. Verify it actually does: force isWorking() to stay
// true (worst case) and assert the thumbnail scrollbar still reflects the
// new page count immediately, not stuck at its pre-merge range.
async function testAppendScrollbarSync(browser) {
  const basePdf = await makeMultiPagePdf(1);
  const appendedPdf = await makeMultiPagePdf(15);

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "base.pdf", mimeType: "application/pdf", buffer: basePdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1500);
  console.log("append-scrollbar document open");

  // simulate a font load that never finishes — the worst case for the
  // checkTasks() gate described above
  await page.evaluate(() => {
    window.__pdfEditor.getPDFDoc().fontLoader.isWorking = () => true;
  });

  const dragState = await page.evaluate((pdfBytes) => {
    const file = new File(
      [new Uint8Array(pdfBytes)], "many.pdf", { type: "application/pdf" });
    const transfer = new DataTransfer();
    transfer.items.add(file);
    window.__appendDropTransfer = transfer;

    const workspace = document.querySelector(".workspace");
    workspace.dispatchEvent(new DragEvent("dragenter", {
      bubbles: true,
      cancelable: true,
      dataTransfer: transfer,
    }));
    const overlay = document.getElementById("pdf-drop-overlay");
    return {
      visible: !overlay.hidden,
      message: document.getElementById("pdf-drop-message").textContent,
    };
  }, Array.from(appendedPdf));
  check("drag overlay announces that the PDF will be appended",
    dragState.visible && dragState.message.includes("anzuhängen"), JSON.stringify(dragState));

  await page.evaluate(() => {
    const workspace = document.querySelector(".workspace");
    workspace.dispatchEvent(new DragEvent("drop", {
      bubbles: true,
      cancelable: true,
      dataTransfer: window.__appendDropTransfer,
    }));
    delete window.__appendDropTransfer;
  });
  await page.waitForFunction(
    () => window.__pdfEditor.getCountPages() === 16, null, { timeout: 30000 });
  await page.waitForTimeout(1000);
  const overlayHidden = await page.evaluate(
    () => document.getElementById("pdf-drop-overlay").hidden);
  check("drag overlay closes after the PDF was dropped", overlayHidden);

  const state = await page.evaluate(() => {
    const th = window.__pdfEditor.getDocumentRenderer().Thumbnails;
    return {
      pages: window.__pdfEditor.getCountPages(),
      thumbPages: th.pages.length,
      thumbScrollMaxY: th.scrollMaxY,
      thumbNeedResize: th.isNeedResize(),
      railOverflow: getComputedStyle(document.getElementById("thumbnails-list")).overflow,
    };
  });
  check("page count reflects the merge", state.pages === 16, `pages=${state.pages}`);
  check("thumbnail sidebar page list reflects the merge", state.thumbPages === 16, `thumbPages=${state.thumbPages}`);
  check("thumbnail scrollbar range updates even while fonts are (simulated) still loading",
    state.thumbScrollMaxY > 0, `thumbScrollMaxY=${state.thumbScrollMaxY}`);
  check("thumbnail sidebar isn't left with a pending resize",
    state.thumbNeedResize === false, `thumbNeedResize=${state.thumbNeedResize}`);
  // Regression guard: the thumbnail rail must never let the BROWSER scroll it
  // natively. The engine draws all pages onto one canvas and scrolls it
  // itself via scrollY/scrollMaxY + its own canvas-drawn scrollbar. If the
  // container's overflow is "auto"/"scroll" and the rendered content is ever
  // taller than the box (seen on some real-world documents), native
  // scrolling silently takes over: it moves what's visible without ever
  // touching the engine's scrollY, so the engine's own scrollbar then looks
  // frozen / out of sync with what's on screen.
  check("thumbnail rail can't be scrolled natively by the browser",
    state.railOverflow === "hidden", `railOverflow=${state.railOverflow}`);

  // Drive a real wheel scroll over the rail and confirm it moves the
  // engine's own scroll state (not just the DOM's native scrollTop).
  const rail = await page.$("#thumbnails-list");
  const railBox = await rail.boundingBox();
  await page.mouse.move(railBox.x + railBox.width / 2, railBox.y + railBox.height / 2);
  for (let i = 0; i < 15; i++) {
    await page.mouse.wheel(0, 150);
    await page.waitForTimeout(40);
  }
  await page.waitForTimeout(300);
  const afterWheel = await page.evaluate(() => {
    const th = window.__pdfEditor.getDocumentRenderer().Thumbnails;
    const rail = document.getElementById("thumbnails-list");
    return { scrollY: th.scrollY, scrollMaxY: th.scrollMaxY, railScrollTop: rail.scrollTop };
  });
  check("mouse-wheel over the thumbnail rail moves the engine's own scroll position",
    afterWheel.scrollY > 0 && afterWheel.scrollY <= afterWheel.scrollMaxY,
    `scrollY=${afterWheel.scrollY} scrollMaxY=${afterWheel.scrollMaxY}`);
  check("the rail's native DOM scroll position never moves (engine owns scrolling)",
    afterWheel.railScrollTop === 0, `railScrollTop=${afterWheel.railScrollTop}`);

  check("no page errors (append-scrollbar scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

// "Teilen"/extract: Stirling-PDF and PDFSam's split-by-range equivalent.
// CPDFDoc.GetPagesBinary(indexes, false) must return a standalone PDF
// containing exactly the requested pages, in order, without mutating the
// open document (no undo entry, page count unchanged afterwards).
async function testExtractPages(browser) {
  const sourcePdf = await makeMultiPagePdf(10);

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "ten.pdf", mimeType: "application/pdf", buffer: sourcePdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1200);
  console.log("extract-pages document open");

  const pageCountBefore = await page.evaluate(() => window.__pdfEditor.getCountPages());

  const downloadPromise = page.waitForEvent("download", { timeout: 15000 });
  await clickTool(page, "pdf-extract");
  await fillPromptDialog(page, "2-4");
  const download = await downloadPromise.catch(() => null);
  check("extracting pages produced a download", !!download);

  if (download) {
    const path = await download.path();
    const bytes = await (await import("node:fs/promises")).readFile(path);
    check("extracted file is a real PDF", bytes.slice(0, 5).toString("latin1") === "%PDF-",
      bytes.slice(0, 8).toString("latin1"));

    const page2 = await browser.newPage();
    page2.on("pageerror", (e) => pageErrors.push(e.message));
    await page2.goto(BASE);
    await page2.waitForFunction(
      () => window.__pdfEditorReady === true, null, { timeout: 90000 });
    await (await page2.$("#file-input")).setInputFiles({ name: "extracted.pdf", mimeType: "application/pdf", buffer: bytes });
    await page2.waitForFunction(
      () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
      null, { timeout: 90000 });
    await page2.waitForTimeout(1000);
    const extractedCount = await page2.evaluate(() => window.__pdfEditor.getCountPages());
    check("extracted PDF contains exactly the requested page range (2-4 → 3 pages)", extractedCount === 3, `pages=${extractedCount}`);
    await page2.close();
  }

  const pageCountAfter = await page.evaluate(() => window.__pdfEditor.getCountPages());
  check("extracting pages never mutates the open document",
    pageCountAfter === pageCountBefore, `before=${pageCountBefore} after=${pageCountAfter}`);

  check("no page errors (extract-pages scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

// "Alle drehen": Stirling-PDF's batch "Rotate PDF" equivalent. One click must
// rotate every page in the document, not just the current one.
async function testRotateAll(browser) {
  const sourcePdf = await makeMultiPagePdf(4);

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "four.pdf", mimeType: "application/pdf", buffer: sourcePdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1200);
  console.log("rotate-all document open");

  const getRotations = () => page.evaluate(() =>
    window.__pdfEditor.getDocumentRenderer().pagesInfo.pages.map((p) => p.GetRotate()));

  const before = await getRotations();
  check("all pages start unrotated", before.every((r) => r === 0), `before=${JSON.stringify(before)}`);

  await clickTool(page, "rotate-all");
  await page.waitForTimeout(500);

  const after = await getRotations();
  check("rotate-all rotates every page, not just the current one",
    after.length === 4 && after.every((r) => r === 90), `after=${JSON.stringify(after)}`);

  check("no page errors (rotate-all scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

// "Entfernen": Stirling-PDF/PDFSam's "remove pages by range" ("2-4,7"),
// distinct from the existing single-current-page delete. Must remove exactly
// the requested pages and stay undoable (Strg+Z restores them all).
async function testRemovePagesByRange(browser) {
  const sourcePdf = await makeMultiPagePdf(6);

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "six.pdf", mimeType: "application/pdf", buffer: sourcePdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1200);
  console.log("remove-pages-range document open");

  await clickTool(page, "page-remove-range");
  await fillPromptDialog(page, "2-3");
  await page.waitForTimeout(500);
  const afterRemove = await page.evaluate(() => window.__pdfEditor.getCountPages());
  check("removing range 2-3 leaves the right page count (6 - 2 = 4)", afterRemove === 4, `pages=${afterRemove}`);

  await clickTool(page, "undo");
  await page.waitForTimeout(500);
  const afterUndo = await page.evaluate(() => window.__pdfEditor.getCountPages());
  check("removing a page range is undoable", afterUndo === 6, `pages=${afterUndo}`);

  check("no page errors (remove-pages-range scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

// UI + new-feature regression: status bar page/zoom controls, menu-based
// page move, ink pen strokes surviving the real save pipeline, and the
// signature pad inserting an image. Guards the modularized toolbar wiring.
async function testUiAndNewTools(browser) {
  const sourcePdf = await makeMultiPagePdf(4);

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "ui.pdf", mimeType: "application/pdf", buffer: sourcePdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1200);
  console.log("ui-and-new-tools document open");

  // status bar controls
  const sc = await page.evaluate(() => ({
    visible: !document.getElementById("status-controls").hidden,
    count: document.getElementById("page-count").textContent,
  }));
  check("status bar shows page controls after open", sc.visible && sc.count === "/ 4", JSON.stringify(sc));
  const mailEnabled = await page.evaluate(
    () => document.getElementById("btn-mail").disabled === false);
  check("mail button is enabled for an open editable PDF", mailEnabled);

  await page.fill("#page-input", "3");
  await page.press("#page-input", "Enter");
  await page.waitForTimeout(600);
  const cur = await page.evaluate(() => window.__pdfEditor.getCurrentPage());
  check("page jump via status bar input", cur === 2, `currentPage=${cur}`);

  await page.selectOption("#zoom-select", "150");
  await page.waitForTimeout(500);
  const zoom = await page.evaluate(() => Math.round(window.__pdfEditor.getDocumentRenderer().zoom * 100));
  check("zoom via status bar select", zoom === 150, `zoom=${zoom}`);
  await page.selectOption("#zoom-select", "100");
  await page.waitForTimeout(400);

  // menu-based page move (current page 3 → position 1), then undo
  await clickTool(page, "page-move");
  await fillPromptDialog(page, "1");
  await page.waitForTimeout(700);
  const moveStatus = await page.evaluate(() => document.getElementById("status").textContent);
  check("page move via Seiten menu", moveStatus.includes("verschoben"), moveStatus);
  await clickTool(page, "undo");
  await page.waitForTimeout(500);

  // ink pen: draw a stroke, leave the mode, save → /Ink annotation persists
  await clickTool(page, "ink");
  await page.waitForTimeout(300);
  const dy = await headerYOffset(page);
  await page.mouse.move(700, 252 + dy);
  await page.mouse.down();
  for (let i = 1; i <= 8; i++) await page.mouse.move(700 + i * 14, 252 + dy + (i % 2) * 22, { steps: 2 });
  await page.mouse.up();
  await page.waitForTimeout(600);
  await clickTool(page, "select");
  await page.waitForTimeout(300);
  const annotTypes = await page.evaluate(() =>
    (window.__pdfEditor.getPDFDoc().annots || []).map((a) => (a.GetType ? a.GetType() : -1)));
  check("ink stroke creates an annotation", annotTypes.length > 0, `types=${JSON.stringify(annotTypes)}`);

  const savedBytes = await page.evaluate(() => {
    const e = window.__pdfEditor;
    const d = e.getPDFDoc();
    const n = e.getCountPages();
    return Array.from(d.GetPagesBinary(Array.from({ length: n }, (_, i) => i), false));
  });
  check("save with ink yields a real PDF",
    Buffer.from(savedBytes.slice(0, 5)).toString() === "%PDF-");
  check("ink annotation serialized into the saved PDF (/Ink)",
    Buffer.from(savedBytes).includes("/Ink"));

  // highlighter: drag over page text must create a visible Highlight
  // annotation. Regression: (a) the viewer opens in hand/pan mode which
  // blocks drag-select (markers silently did nothing), (b) marker opacity is
  // 0-100 in the engine — passing 1 made highlights invisible (1%).
  await clickTool(page, "highlight");
  await page.waitForTimeout(300);
  await page.mouse.move(400, 120 + dy);
  await page.mouse.down();
  await page.mouse.move(800, 120 + dy, { steps: 10 });
  await page.mouse.up();
  await page.waitForTimeout(800);
  const highlight = await page.evaluate(() => {
    const d = window.__pdfEditor.getPDFDoc();
    const annots = (d.annots || []).filter((a) => a.GetType && a.GetType() === window.AscPDF.ANNOTATIONS_TYPES.Highlight);
    return {
      count: annots.length,
      opacity: annots.length ? annots[annots.length - 1].GetOpacity() : null,
      stillArmed: window.__pdfEditor.isMarkerFormat === true,
    };
  });
  check("highlighter drag creates a Highlight annotation",
    highlight.count > 0, JSON.stringify(highlight));
  check("highlight is visible (opacity ~0.5, not 1%)",
    highlight.opacity !== null && highlight.opacity > 0.2 && highlight.opacity <= 1,
    `opacity=${highlight.opacity}`);
  check("marker stays armed for repeated use", highlight.stillArmed);
  await clickTool(page, "select");
  await page.waitForTimeout(300);

  // signature pad: Shift+click opens it even with a stored signature
  await page.keyboard.down("Shift");
  await page.click('[data-tool="signature"]');
  await page.keyboard.up("Shift");
  await page.waitForSelector("#signature-dialog:not([hidden])", { timeout: 5000 });
  const cb = await (await page.$("#signature-canvas")).boundingBox();
  await page.mouse.move(cb.x + 60, cb.y + 120);
  await page.mouse.down();
  await page.mouse.move(cb.x + 220, cb.y + 70, { steps: 6 });
  await page.mouse.up();
  await page.click("#signature-ok");
  await page.waitForTimeout(1200);
  const sigStatus = await page.evaluate(() => document.getElementById("status").textContent);
  check("signature pad stores and inserts the signature", /Signatur/.test(sigStatus), sigStatus);

  check("no page errors (ui-and-new-tools scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

// Shapes drawn with the mouse must (a) actually appear (StartAddShape's
// is_apply flag ARMS with true and CANCELS with false — regression guard for
// getting it backwards) and (b) survive the real save path. The engine's
// save stream has no drawing serialization, so the host bakes shapes into
// the PDF with pdf-lib; this verifies that end to end via pixel comparison.
async function testShapesSurviveSave(browser) {
  const sourcePdf = await makeMultiPagePdf(1);

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));
  page.on("dialog", (d) => d.accept());

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "shapes.pdf", mimeType: "application/pdf", buffer: sourcePdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1500);
  console.log("shapes document open");

  const dy = await headerYOffset(page);
  const shapeDrags = [
    ["shape", 420, 200, 560, 280],
    ["shape-ellipse", 600, 200, 720, 280],
    ["shape-line", 420, 330, 700, 360],
    ["shape-arrow", 420, 400, 700, 440],
  ];
  for (const [tool, x1, y1, x2, y2] of shapeDrags) {
    await clickTool(page, tool);
    await page.waitForTimeout(200);
    await page.mouse.move(x1, y1 + 52 + dy);
    await page.mouse.down();
    await page.mouse.move(x2, y2 + 52 + dy, { steps: 8 });
    await page.mouse.up();
    await page.waitForTimeout(500);
  }
  const drawn = await page.evaluate(() => {
    const d = window.__pdfEditor.getPDFDoc();
    try { return d.GetPageInfo(0).drawings.filter((x) => x.IsShape && x.IsShape()).length; }
    catch { return 0; }
  });
  check("all four shape presets draw via mouse drag", drawn === 4, `shapes=${drawn}`);

  const bandCount = (p) => p.evaluate(() => {
    const e = window.__pdfEditor;
    const d = e.getPDFDoc();
    const r = e.getDocumentRenderer();
    const w = Math.round(d.GetPageWidthMM(0) / 25.4 * 96);
    const h = Math.round(d.GetPageHeightMM(0) / 25.4 * 96);
    const canvas = r.GetPrintPage(0, w, h, window.AscPDF.PRINT_CONTENT_TYPES.docAndMarkups);
    const ctx = canvas.getContext("2d");
    const img = ctx.getImageData(0, Math.round(h * 0.15), w, Math.round(h * 0.5)).data;
    let nonWhite = 0;
    for (let i = 0; i < img.length; i += 4) {
      if (img[i] < 245 || img[i + 1] < 245 || img[i + 2] < 245) nonWhite++;
    }
    return nonWhite;
  });

  const pixelsBefore = await bandCount(page);
  const downloadPromise = page.waitForEvent("download", { timeout: 60000 });
  await page.click("#btn-save");
  const download = await downloadPromise;
  const stream = await download.createReadStream();
  const chunks = [];
  for await (const c of stream) chunks.push(c);
  const saved = Buffer.concat(chunks);
  check("saving with shapes yields a real PDF", saved.slice(0, 5).toString() === "%PDF-", `${saved.length} bytes`);

  const page2 = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page2.on("dialog", (d) => d.accept());
  await page2.goto(BASE);
  await page2.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page2.$("#file-input")).setInputFiles({ name: "shapes-saved.pdf", mimeType: "application/pdf", buffer: saved });
  await page2.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page2.waitForTimeout(1500);
  const pixelsAfter = await bandCount(page2);
  check("shapes survive save+reopen (baked into the PDF)",
    pixelsBefore > 500 && pixelsAfter >= pixelsBefore * 0.6,
    `before=${pixelsBefore} after=${pixelsAfter}`);

  check("no page errors (shapes scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
  await page2.close();
}

// Hand tool, marker color, and — critically — opening a SECOND document.
// The handover stashes bytes in IndexedDB and reloads; a version mismatch
// between two openers of the same DB (VersionError) once broke it silently,
// and no scenario covered it because each test opens exactly one file.
async function testHandMarkerSecondDoc(browser) {
  const first = await makeMultiPagePdf(2);
  const second = await makeMultiPagePdf(5);

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));
  page.on("dialog", (d) => d.accept());

  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "erste.pdf", mimeType: "application/pdf", buffer: first });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1200);
  console.log("hand-marker-seconddoc document open");

  // hand tool pans, select restores drag-to-select
  await clickTool(page, "hand");
  const handMode = await page.evaluate(() => !!window.__pdfEditor.getDocumentRenderer().MouseHandObject);
  check("hand tool switches the engine into pan mode", handMode);
  await clickTool(page, "select");
  const selectMode = await page.evaluate(() => !window.__pdfEditor.getDocumentRenderer().MouseHandObject);
  check("select tool restores text-selection mode", selectMode);

  // marker color picker feeds the engine
  await page.evaluate(() => {
    const input = document.getElementById("marker-color");
    input.value = "#00c853";
    input.dispatchEvent(new Event("change"));
  });
  await clickTool(page, "highlight");
  await page.waitForTimeout(300);
  const hlColor = await page.evaluate(() => window.__pdfEditor.getPDFDoc().HighlightColor);
  check("marker color picker feeds SetMarkerFormat", 
    !!hlColor && hlColor.r === 0 && hlColor.g === 200 && hlColor.b === 83, JSON.stringify(hlColor));
  await clickTool(page, "select");

  // second document: IndexedDB handover + reload must survive
  await (await page.$("#file-input")).setInputFiles({ name: "zweite.pdf", mimeType: "application/pdf", buffer: second });
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      await page.waitForFunction(
        () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten")
          && document.getElementById("status").textContent.includes("zweite"),
        null, { timeout: 90000 });
      break;
    } catch (e) {
      if (!/Execution context was destroyed|navigat/i.test(String(e))) throw e;
    }
  }
  await page.waitForTimeout(1500);
  const pagesAfter = await page.evaluate(() => window.__pdfEditor.getCountPages());
  check("opening a second document survives the IndexedDB handover", pagesAfter === 5, `pages=${pagesAfter}`);

  // Leaving the Text tool without real edits must roll back the page
  // "recognition" (it permanently kills viewer-layer text selection);
  // WITH real edits the object model must survive the tool switch.
  const dy2 = await headerYOffset(page);
  await clickTool(page, "edit-text");
  await page.waitForTimeout(1200);
  await page.evaluate(() => window.__pdfEditor.getDocumentRenderer().navigateToPage(1));
  await page.waitForFunction(() => {
    const editor = window.__pdfEditor;
    const view = editor.getDocumentRenderer();
    return editor.getCurrentPage() === 1
      && view.file.pages[1] && view.file.pages[1].isRecognized;
  }, null, { timeout: 90000 });
  const documentWideTextMode = await page.evaluate(() => ({
    active: document.querySelector('[data-tool="edit-text"]').classList.contains("active"),
    page: window.__pdfEditor.getCurrentPage(),
  }));
  check("Text tool remains active and prepares the next page automatically",
    documentWideTextMode.active && documentWideTextMode.page === 1,
    JSON.stringify(documentWideTextMode));
  await page.evaluate(() => window.__pdfEditor.getDocumentRenderer().navigateToPage(0));
  await page.waitForFunction(() => window.__pdfEditor.getCurrentPage() === 0);
  await page.waitForTimeout(300);
  await clickTool(page, "select");
  await page.waitForTimeout(400);
  await page.mouse.move(400, 120 + 52 + dy2);
  await page.mouse.down();
  await page.mouse.move(800, 120 + 52 + dy2, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(500);
  const quadsAfterPeek = await page.evaluate(() =>
    window.__pdfEditor.getDocumentRenderer().file.getSelectionQuads().length);
  check("text selection works again after LEAVING the Text tool (peek)",
    quadsAfterPeek > 0, `quads=${quadsAfterPeek}`);

  // REAL edits remain in ONLYOFFICE's drawing text model across tool changes.
  // Marking a selection must not reload/OCR the page, and the same text object
  // must accept a second character-level edit afterwards.
  await clickTool(page, "edit-text");
  await page.waitForTimeout(1500);
  await page.mouse.dblclick(430, 120 + 52 + dy2);
  await page.waitForTimeout(1200);
  await page.keyboard.press("End");
  await page.keyboard.type("XQY", { delay: 80 });
  await page.waitForTimeout(500);

  await page.evaluate(() => {
    const doc = window.__pdfEditor.getPDFDoc();
    window.__editCycleDoc = doc;
    window.__editCycleObject = doc.GetActiveObject();
  });
  await page.keyboard.down("Shift");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.up("Shift");
  await clickTool(page, "highlight");
  await page.waitForTimeout(500);

  const markerState = await page.evaluate(() => {
    const doc = window.__pdfEditor.getPDFDoc();
    return {
      sameDocument: doc === window.__editCycleDoc,
      sameObject: doc.GetActiveObject() === window.__editCycleObject,
      status: document.getElementById("status").textContent,
    };
  });
  check("marking edited text does not reload the document",
    markerState.sameDocument && markerState.sameObject,
    `sameDocument=${markerState.sameDocument} sameObject=${markerState.sameObject}`);
  check("highlight is applied inside the editable text model",
    markerState.status.includes("Textformatierung angewendet"), markerState.status);

  // Entf while the editable marker is active clears only the formatting. The
  // selected text and its drawing object must remain available for more edits.
  await page.keyboard.press("Delete");
  await page.waitForTimeout(300);
  const clearedMarkerState = await page.evaluate(() => {
    const doc = window.__pdfEditor.getPDFDoc();
    const content = doc.GetController().getTargetDocContent(undefined, true);
    const para = content && content.GetCurrentParagraph && content.GetCurrentParagraph();
    let value = "";
    if (para) {
      para.CheckRunContent((run) => {
        for (const item of run.Content || []) {
          if (!item.IsText || !item.IsText()) continue;
          const cp = item.GetCodePoint ? item.GetCodePoint() : item.Value;
          if (Number.isFinite(cp)) value += String.fromCodePoint(cp);
        }
      });
    }
    return {
      sameObject: doc.GetActiveObject() === window.__editCycleObject,
      text: value,
      status: document.getElementById("status").textContent,
    };
  });
  check("Delete removes editable highlight without deleting its text",
    clearedMarkerState.sameObject
      && clearedMarkerState.text.includes("XQY")
      && clearedMarkerState.status.includes("Markierung entfernt"),
    `sameObject=${clearedMarkerState.sameObject} text=${clearedMarkerState.text} status=${clearedMarkerState.status}`);

  await clickTool(page, "edit-text");
  await page.waitForTimeout(250);
  await page.keyboard.press("End");
  await page.keyboard.type("Z", { delay: 80 });
  await page.waitForTimeout(500);
  const textAfterSecondEdit = await page.evaluate(() => {
    const doc = window.__pdfEditor.getPDFDoc();
    const content = doc.GetController().getTargetDocContent(undefined, true);
    const para = content && content.GetCurrentParagraph && content.GetCurrentParagraph();
    let value = "";
    if (para) {
      para.CheckRunContent((run) => {
        for (const item of run.Content || []) {
          if (!item.IsText || !item.IsText()) continue;
          const cp = item.GetCodePoint ? item.GetCodePoint() : item.Value;
          if (Number.isFinite(cp)) value += String.fromCodePoint(cp);
        }
      });
    }
    return value;
  });
  check("marked text accepts a second character-level edit",
    textAfterSecondEdit.includes("XQYZ"), textAfterSecondEdit);
  const dirtyAfterEdit = await page.evaluate(() => document.title.startsWith("•"));
  check("document stays marked unsaved after editable formatting", dirtyAfterEdit);

  check("no page errors (hand-marker-seconddoc scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

// Searchable-scan OCR: only when the OCR stack is vendored (npm run
// fetch-ocr) — CI fetches it, a bare checkout skips gracefully.
async function testOcrSearchable(browser) {
  try {
    const head = await fetch(`${BASE}/vendor/ocr/tesseract.min.js`, { method: "HEAD" });
    if (!head.ok) throw new Error();
  } catch {
    console.log("ocr-searchable: vendor/ocr missing — skipped");
    return;
  }

  // scan-like PDF: text rendered into an IMAGE, no text layer
  const shotPage = await browser.newPage({ viewport: { width: 1240, height: 1754 } });
  await shotPage.setContent(`<div style="width:1240px;height:1754px;background:#fff;font-family:serif;padding:80px;box-sizing:border-box">
    <h1 style="font-size:48px">Vertrag über die Lieferung</h1>
    <p style="font-size:28px">Der Auftragnehmer verpflichtet sich zur fristgerechten Lieferung.</p>
  </div>`);
  const shot = await shotPage.screenshot({ type: "png" });
  await shotPage.close();
  const { PDFDocument } = await import("pdf-lib");
  const scanDoc = await PDFDocument.create();
  const png = await scanDoc.embedPng(shot);
  scanDoc.addPage([595, 842]).drawImage(png, { x: 0, y: 0, width: 595, height: 842 });
  const scanPdf = Buffer.from(await scanDoc.save());

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));
  page.on("dialog", (d) => d.accept());
  await page.goto(BASE);
  await page.waitForFunction(
    () => window.__pdfEditorReady === true, null, { timeout: 90000 });
  await (await page.$("#file-input")).setInputFiles({ name: "scan.pdf", mimeType: "application/pdf", buffer: scanPdf });
  await page.waitForFunction(
    () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
    null, { timeout: 90000 });
  await page.waitForTimeout(1500);
  console.log("ocr-searchable document open");

  const countMatches = () => page.evaluate(() => {
    const props = new window.AscCommon.CSearchSettings();
    props.put_Text("Lieferung");
    props.put_MatchCase(false);
    const n = window.__pdfEditor.asc_findText(props, true) | 0;
    try { window.__pdfEditor.asc_endFindText(); } catch { /* none */ }
    return n;
  });
  check("scan has no searchable text before OCR", (await countMatches()) === 0);

  await clickTool(page, "ocr");
  await fillPromptDialog(page, "1");
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      await page.waitForFunction(
        () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
        null, { timeout: 300000 });
      break;
    } catch (e) {
      if (!/Execution context was destroyed|navigat/i.test(String(e))) throw e;
    }
  }
  await page.waitForTimeout(2000);
  const matches = await countMatches();
  check("scan is searchable after OCR (invisible text layer)", matches >= 1, `matches=${matches}`);

  check("no page errors (ocr-searchable scenario)", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  await page.close();
}

async function main() {
  console.log("=== PDF editor typing smoke test ===\n");

  // 1. Start the dev server
  const server = spawn(process.execPath, [join(ROOT, "server.mjs")], {
    env: { ...process.env, PORT },
    stdio: "ignore",
  });
  const stopServer = () => { try { server.kill(); } catch { /* already dead */ } };
  process.on("exit", stopServer);
  await waitForServer(`${BASE}/`);

  const browser = await launchChromium();
  const work = await mkdtemp(join(tmpdir(), "pdf-smoke-"));
  const pdfPath = join(work, "test.pdf");

  try {
    // 2. Create a test PDF with embedded subset fonts. printToPDF subsets the
    //    fonts, so the headline font contains ONLY its own characters — the
    //    text typed later deliberately uses characters outside that subset.
    {
      const page = await browser.newPage();
      await page.setContent(`<!doctype html><html><body style="font-family: serif">
        <h1>Gemeinsames Amtsblatt des Landes</h1>
        <p style="font-size:14px">Bekanntmachung des Ministeriums des Inneren</p>
        <p style="font-family: monospace">Ausgabe 6 / 2026</p>
      </body></html>`);
      await page.pdf({ path: pdfPath, format: "A4" });
      await page.close();
    }

    // 3. Open the editor
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));

    await page.goto(BASE);
    await page.waitForFunction(
      () => window.__pdfEditorReady === true,
      null, { timeout: 90000 }
    );
    console.log("editor ready");

    // 4. Font-name resolution must be backed by the selection table
    const resolution = await page.evaluate(() => {
      const app = window.AscFonts.g_fontApplication;
      const out = { listLen: app.g_fontSelections.List.length, names: {} };
      for (const n of ["Arial", "Times New Roman", "Courier New", "DejaVu Serif",
        "BAAAAA+DejaVuSerifCondensed-Bold", "CompletelyUnknownFont"]) {
        out.names[n] = app.GetFontFileWeb(n).m_wsFontName;
      }
      return out;
    });
    check("selection table loaded", resolution.listLen > 100, `${resolution.listLen} records`);
    for (const [name, resolved] of Object.entries(resolution.names)) {
      check(`"${name}" resolves to a real font`, resolved !== "ASCW3", `→ "${resolved}"`);
    }
    check(`Arial resolves to Liberation Sans`, resolution.names["Arial"] === "Liberation Sans",
      `→ "${resolution.names["Arial"]}"`);
    check(`Times New Roman resolves to Liberation Serif`,
      resolution.names["Times New Roman"] === "Liberation Serif",
      `→ "${resolution.names["Times New Roman"]}"`);

    // 5. Open the PDF and type into the subset-font headline
    const input = await page.$("#file-input");
    await input.setInputFiles(pdfPath);
    await page.waitForFunction(
      () => document.getElementById("status").textContent.includes("bereit zum Bearbeiten"),
      null, { timeout: 90000 }
    );
    console.log("document open");
    await page.waitForTimeout(2000);

    await clickTool(page, "edit-text");
    await page.waitForTimeout(1500);
    // double-click into the headline (fixed viewport → stable coordinates)
    await page.mouse.dblclick(590, 111 + await headerYOffset(page));
    await page.waitForTimeout(1500);
    await page.keyboard.press("End");

    // "Byrußé" — none of these characters are in the headline's subset
    const TYPED = "Byrußé";
    await page.keyboard.type(" " + TYPED, { delay: 60 });
    await page.waitForTimeout(2000);

    // 6. Every typed character must have its own real grapheme. The artifact
    //    bug collapsed all missing characters onto ONE shared .notdef
    //    grapheme of the embedded font.
    const glyphs = await page.evaluate((typed) => {
      const e = window.__pdfEditor;
      const para = e.getPDFDoc().getTextController().GetDocContent().GetCurrentParagraph();
      const wanted = new Set([...typed].map((c) => c.codePointAt(0)));
      const found = [];
      para.CheckRunContent((run) => {
        for (const item of run.Content) {
          if (!item.IsText || !item.IsText()) continue;
          const cp = item.GetCodePoint ? item.GetCodePoint() : item.Value;
          if (wanted.has(cp)) found.push({ ch: String.fromCodePoint(cp), grapheme: item.Grapheme });
        }
      });
      return found;
    }, TYPED);

    check("all typed characters reached the document",
      glyphs.length === TYPED.length, `${glyphs.length}/${TYPED.length}`);
    check("every typed character has a grapheme",
      glyphs.every((g) => typeof g.grapheme === "number" && g.grapheme > 0),
      glyphs.map((g) => `${g.ch}:${g.grapheme}`).join(" "));
    const distinct = new Set(glyphs.map((g) => g.grapheme));
    check("distinct characters shaped to distinct graphemes (no shared .notdef)",
      distinct.size === glyphs.length, `${distinct.size} graphemes for ${glyphs.length} chars`);

    // 7. Saving must produce a real PDF. GetPagesBinary runs the WASM
    //    serializer; viewer.Save() only returns the change-command stream —
    //    an earlier regression shipped exactly that as ".pdf".
    const savedHead = await page.evaluate(() => {
      const e = window.__pdfEditor;
      const doc = e.getPDFDoc();
      const n = e.getCountPages() | 0;
      const bytes = doc.GetPagesBinary(Array.from({ length: n }, (_, i) => i), false);
      return bytes ? { head: String.fromCharCode(...bytes.slice(0, 5)), len: bytes.length } : null;
    });
    check("save produces a real PDF",
      !!savedHead && savedHead.head === "%PDF-" && savedHead.len > 1000,
      savedHead ? `${savedHead.head}… ${savedHead.len} bytes` : "no bytes");

    check("no page errors", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));

    await page.close();

    // ── Scenario 2: form fill round-trip ─────────────────────────────────
    // Fill an AcroForm (text field + checkbox) in fill mode, save through
    // the real serializer, reopen the produced bytes and verify the values.
    // Guards the fill-mode gating (OnlyForms restriction), the
    // checkFieldFont/loadedFonts first-keystroke fix and the save pipeline.
    await testFormRoundtrip(browser);
    await testAppendScrollbarSync(browser);
    await testExtractPages(browser);
    await testRotateAll(browser);
    await testRemovePagesByRange(browser);
    await testUiAndNewTools(browser);
    await testShapesSurviveSave(browser);
    await testHandMarkerSecondDoc(browser);
    await testOcrSearchable(browser);
    await testEditPageImageRendering(browser);
    await testWatermark(browser);
    await testTextModeSaveKeepsContent(browser);
    await testPageNumbers(browser);
    await testExtractEmbeddedImages(browser);
    await testExtractEmbeddedImagesNone(browser);
    await testExportPagesAsImages(browser);
  } finally {
    await browser.close();
    await rm(work, { recursive: true, force: true });
    stopServer();
  }

  if (failures > 0) {
    console.error(`\n✗ ${failures} check(s) FAILED`);
    process.exit(1);
  }
  console.log("\n✓ all checks passed");
}

main().catch((err) => {
  console.error("smoke test crashed:", err);
  process.exit(1);
});
