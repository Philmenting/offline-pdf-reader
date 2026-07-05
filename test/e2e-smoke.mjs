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
    await page.mouse.dblclick(590, 111);
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
