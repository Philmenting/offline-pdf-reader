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

// Click coordinates below are calibrated for a 52px-high header at 100% zoom
// in a 1280px viewport. The toolbar may wrap to more rows (taller header), so
// shift all page-area Y coordinates by the actual header growth.
async function headerYOffset(page) {
  const h = await page.evaluate(() => document.querySelector(".app-header").getBoundingClientRect().height);
  return Math.round(h - 52);
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
    () => document.getElementById("status").textContent.includes("Bereit"), null, { timeout: 90000 });
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
  await page.click('[data-tool="form-fill"]');
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
    () => document.getElementById("status").textContent.includes("Bereit"), null, { timeout: 90000 });
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

  const chooser = page.waitForEvent("filechooser", { timeout: 15000 });
  await page.click('[data-tool="pdf-append"]');
  await (await chooser).setFiles({ name: "many.pdf", mimeType: "application/pdf", buffer: appendedPdf });
  await page.waitForTimeout(2500); // same order of wait a user would give it

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
      () => document.getElementById("status").textContent.includes("Bereit"),
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

    await page.click('[data-tool="edit-text"]');
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
