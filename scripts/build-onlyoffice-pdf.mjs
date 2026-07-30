#!/usr/bin/env node
/**
 * Builds the ONLYOFFICE sdkjs PDF editor engine from source and vendors it.
 *
 * The PDF editor's engine is the sdkjs "word" product: its config pulls in all
 * 57 `pdf/src/*` modules (viewer, document, annotations, forms, drawings) plus
 * the shared engine. The upstream build is concatenation-only (no Java/closure
 * compiler), so it runs anywhere Python 3 is available.
 *
 * Steps:
 *   1. Download the sdkjs source tarball at a pinned commit (cached).
 *   2. Extract it.
 *   3. Run `python3 build/build.py --product word`.
 *   4. Copy deploy/sdkjs -> vendor/onlyoffice/sdkjs.
 *
 * Output is AGPL-3.0 (Copyright Ascensio System SIA); see NOTICE. It is
 * gitignored and produced on demand.
 *
 * Usage: node scripts/build-onlyoffice-pdf.mjs
 */
import { mkdir, rm, stat, cp, writeFile, readFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { createWriteStream } from "node:fs";
import { Readable } from "node:stream";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");

// Pinned ONLYOFFICE/sdkjs commit (master @ 2026-05-19).
const SDKJS_REF = "72b0421c0bbf9d01eed9cf14834ae47eb2df1b50";
const TARBALL = `https://codeload.github.com/ONLYOFFICE/sdkjs/tar.gz/${SDKJS_REF}`;

const WORK = join(ROOT, ".build");
const TAR_PATH = join(WORK, `sdkjs-${SDKJS_REF.slice(0, 10)}.tar.gz`);
const SRC_DIR = join(WORK, "sdkjs");
const VENDOR = join(ROOT, "vendor", "onlyoffice");

function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, args, { stdio: "inherit", ...opts });
    p.on("error", reject);
    p.on("close", (code) =>
      code === 0 ? resolve() : reject(new Error(`${cmd} exited ${code}`))
    );
  });
}

async function exists(p) {
  try { await stat(p); return true; } catch { return false; }
}

/**
 * Patch the prebuilt drawingfile.js (WASM wrapper) for engine/viewer.js compat.
 *
 * At the pinned commit the standalone engine bundle `engine/viewer.js` still
 * calls `this.V.memory()` on the CDrawingFile to read a page pixmap out of the
 * WASM heap (the 2D, non-WebGL render path: `new Uint8ClampedArray(
 * this.memory().buffer, ptr, 4*w*h)`). The matching `engine/drawingfile.js`
 * dropped that method in favour of `getUint8ClampedArray`, so on machines where
 * WebGL is unavailable (e.g. our Electron shell) rendering crashes with
 * "this.V.memory is not a function".
 *
 * Re-add a `memory()` method returning the heap typed array, whose `.buffer` is
 * exactly the ArrayBuffer the engine indexes into. `Module["HEAP8"]` is in scope
 * (same IIFE) and is reassigned on every WASM memory growth, so reading it at
 * call time always yields the current buffer.
 *
 * Additionally, _InitializeFonts has an early-return when g_fonts_selection_bin
 * is falsy (we set it to "" since we have no precomputed selection table). That
 * early-return also skips _InitializeFontsRanges, which the WASM engine needs
 * to map Unicode codepoints to font files. Without ranges, every page's font
 * requirements stay "pending" and getPagePixmap returns null → blank pages.
 * We patch the early-return so it still initialises the base path and symbol
 * ranges even when there is no selection-bin data.
 */
/**
 * Harden CTextShaper.FlushWord against a null font file (word/sdk-all.js).
 *
 * When a paragraph is shaped while its font FILE is not yet in memory
 * (SetFontInternal returned null — e.g. a saved form's DA references the
 * serializer's "dummy__noop" placeholder font right after open), FlushWord
 * dereferences this.FontId.m_pFaceInfo and throws, aborting the WHOLE page
 * recalculation: the document opens blank. Skipping the word instead is
 * safe — the async font load completes moments later and the follow-up
 * repaint shapes it correctly.
 */
async function patchTextShaper() {
  const file = join(VENDOR, "sdkjs", "word", "sdk-all.js");
  if (!(await exists(file))) return;
  let src = await readFile(file, "utf8");

  const needle = "\t\tlet oFontInfo = this.GetFontInfo(this.FontSlot);\n" +
    "\t\tlet nFontId   = AscCommon.FontNameMap.GetId(this.FontId.m_pFaceInfo.family_name);";
  const guard = "\t\tif (!this.FontId || !this.FontId.m_pFaceInfo)\n" +
    "\t\t\treturn this.ClearBuffer(); // font file not in memory yet — repaint after load reshapes\n";

  if (!src.includes(needle)) {
    console.warn("  ! patchTextShaper: FlushWord signature not found — upstream changed, patch skipped");
    return;
  }
  src = src.replace(needle, guard + needle);
  await writeFile(file, src);
  console.log("→ patched sdk-all.js: FlushWord null-font guard");
}

/**
 * Keep AcroForm field names as metadata without painting them onto the page.
 *
 * In edit mode ONLYOFFICE creates a temporary transparent shape for every
 * widget and writes GetFullName() into that shape. Those are the black
 * "Text1" and "DatumRow1" labels users see over otherwise valid fields.
 * This editor fills existing forms but does not redesign their widgets, so
 * the temporary shape must stay visually empty.
 */
async function patchFormDesignLabels() {
  const file = join(VENDOR, "sdkjs", "word", "sdk-all.js");
  if (!(await exists(file))) return;
  let src = await readFile(file, "utf8");

  const needle = "oRun.AddText(this.GetFullName());";
  const count = src.split(needle).length - 1;
  if (count !== 2) {
    throw new Error(
      `patchFormDesignLabels: expected 2 occurrences, found ${count}; upstream form rendering changed`);
  }

  src = src.replaceAll(needle,
    'oRun.AddText(""); // patched: internal AcroForm names are metadata, not visible page text');
  await writeFile(file, src);
  console.log("→ patched sdk-all.js: internal form field names remain visually hidden");
}

/**
 * Stop the save pipeline from BLANKING pages that were opened in text-edit
 * mode ("Text" tool → CPDFDoc.EditPage() marks the page isRecognized).
 *
 * Both change-stream writers (CHtmlPage.Save and CHtmlPage.SaveForSplit)
 * emit a ctPageClear command for recognized pages, expecting the page's
 * drawings to be re-serialized afterwards. But SaveForSplit — the ONLY
 * writer this app can use, via nativeFile.SplitPages (there is no other
 * PDF-producing WASM entry point) — never writes drawings, and the WASM
 * split writer has no code path for shape commands anyway (feeding it
 * Save()'s drawing frames traps with "null function or function signature
 * mismatch"). Net effect upstream: open a PDF, click "Text", save → the
 * page is cleared and nothing is written back. Silent, total data loss.
 *
 * Never clearing keeps the ORIGINAL page content in the saved file. The
 * trade-off: text-mode edits themselves still can't be persisted by this
 * standalone build (an engine/WASM limitation), but saving no longer
 * destroys pages.
 */
async function patchSaveNoPageClear() {
  const file = join(VENDOR, "sdkjs", "word", "sdk-all.js");
  if (!(await exists(file))) return;
  let src = await readFile(file, "utf8");

  const needle = "let bClearPage = !!oFile.pages[curIndex].isRecognized;";
  const count = src.split(needle).length - 1;
  if (count !== 2) {
    console.warn(`  ! patchSaveNoPageClear: expected 2 occurrences, found ${count} — upstream changed, patch skipped`);
    return;
  }
  src = src.replaceAll(needle,
    "let bClearPage = false; // patched: split-save can't rewrite drawings, so never clear (see build script)");
  await writeFile(file, src);
  console.log("→ patched sdk-all.js: save no longer clears text-edited pages");
}

async function patchDrawingFile() {
  const file = join(VENDOR, "sdkjs", "pdf", "src", "engine", "drawingfile.js");
  if (!(await exists(file))) return;
  let src = await readFile(file, "utf8");

  // Patch 1: re-add memory() method
  const anchor = 'self["AscViewer"]["CDrawingFile"]=CFile;';
  if (!src.includes(anchor)) {
    console.warn("⚠ drawingfile.js: patch anchor not found; skipping");
    return;
  }
  if (!src.includes('CFile.prototype["memory"]')) {
    src = src.replace(
      anchor,
      'CFile.prototype["memory"]=function(){return Module["HEAP8"]};' + anchor
    );
    console.log("→ patched drawingfile.js: re-added CDrawingFile.memory()");
  }

  // Patch 2: fix _InitializeFonts early-return skipping font ranges
  const oldGuard = 'if(!window["g_fonts_selection_bin"])return;';
  if (src.includes(oldGuard)) {
    const oldBlock =
      'if(!window["g_fonts_selection_bin"])return;' +
      'var memoryBuffer=window["g_fonts_selection_bin"].toUtf8();' +
      'var pointer=Module["_malloc"](memoryBuffer.length);' +
      'Module.HEAP8.set(memoryBuffer,pointer);' +
      'Module["_InitializeFontsBase64"](pointer,memoryBuffer.length);' +
      'Module["_free"](pointer);' +
      'delete window["g_fonts_selection_bin"];';
    const newBlock =
      'console.log("[engine] _InitializeFonts entered, g_fonts_selection_bin="+typeof window["g_fonts_selection_bin"]+" len="+(window["g_fonts_selection_bin"]?window["g_fonts_selection_bin"].length:0));' +
      'if(window["g_fonts_selection_bin"]){' +
      'var memoryBuffer=window["g_fonts_selection_bin"].toUtf8();' +
      'var pointer=Module["_malloc"](memoryBuffer.length);' +
      'Module.HEAP8.set(memoryBuffer,pointer);' +
      'Module["_InitializeFontsBase64"](pointer,memoryBuffer.length);' +
      'Module["_free"](pointer);' +
      'delete window["g_fonts_selection_bin"];}';
    if (src.includes(oldBlock)) {
      src = src.replace(oldBlock, newBlock);
      console.log("→ patched drawingfile.js: _InitializeFonts no longer skips font ranges");
    } else {
      console.warn("⚠ drawingfile.js: _InitializeFonts code block not matched; skipping ranges patch");
    }
  }

  // Patch 3: log _InitializeFontsRanges execution
  const rangesCall = 'Module["_InitializeFontsRanges"]';
  if (src.includes(rangesCall) && !src.includes('[engine] _InitializeFontsRanges')) {
    src = src.replace(
      rangesCall,
      'console.log("[engine] _InitializeFontsRanges called");' + rangesCall
    );
    console.log("→ patched drawingfile.js: added _InitializeFontsRanges logging");
  }

  // Patch 4: log font request via CheckStreamId public API
  const checkStreamPublic = 'self["AscViewer"]["CheckStreamId"]=function(data,status){return CFile.prototype._CheckStreamId(data,status)}';
  if (src.includes(checkStreamPublic) && !src.includes('[engine] CheckStreamId')) {
    src = src.replace(
      checkStreamPublic,
      'self["AscViewer"]["CheckStreamId"]=function(data,status){console.log("[engine] CheckStreamId status="+status);return CFile.prototype._CheckStreamId(data,status)}'
    );
    console.log("→ patched drawingfile.js: added CheckStreamId logging");
  }

  // Patch 5: log getPagePixmap null returns (blank pages)
  const getPixmapAnchor = 'CFile.prototype["getPagePixmap"]=function';
  if (src.includes(getPixmapAnchor) && !src.includes('[engine] getPagePixmap')) {
    src = src.replace(
      getPixmapAnchor,
      'CFile.prototype["getPagePixmap"]=function(){' +
      'var r=this.__origGetPagePixmap.apply(this,arguments);' +
      'if(!r)console.log("[engine] getPagePixmap returned null for page="+arguments[0]+" pendingFonts="+(this.l&&this.l[arguments[0]]&&this.l[arguments[0]].fonts?this.l[arguments[0]].fonts.length:"?"));' +
      'return r;};' +
      'CFile.prototype["__origGetPagePixmap"]=function'
    );
    console.log("→ patched drawingfile.js: added getPagePixmap null-return logging");
  }

  await writeFile(file, src, "utf8");
}

async function downloadTarball() {
  if (await exists(TAR_PATH)) {
    console.log(`✓ cached tarball: ${TAR_PATH}`);
    return;
  }
  console.log(`↓ downloading sdkjs @ ${SDKJS_REF.slice(0, 10)} ...`);
  const res = await fetch(TARBALL);
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${TARBALL}`);
  await new Promise((resolve, reject) => {
    const out = createWriteStream(TAR_PATH);
    Readable.fromWeb(res.body).pipe(out);
    out.on("finish", resolve);
    out.on("error", reject);
  });
  console.log(`✓ saved ${TAR_PATH}`);
}

async function main() {
  await mkdir(WORK, { recursive: true });
  await downloadTarball();

  console.log("→ extracting source ...");
  await rm(SRC_DIR, { recursive: true, force: true });
  await mkdir(SRC_DIR, { recursive: true });
  await run("tar", ["xzf", TAR_PATH, "-C", SRC_DIR, "--strip-components=1"]);

  console.log("→ building 'word' product (PDF editor engine) ...");
  await run("python3", ["build/build.py", "--product", "word"], { cwd: SRC_DIR });

  console.log("→ vendoring deploy/sdkjs -> vendor/onlyoffice/sdkjs ...");
  await rm(join(VENDOR, "sdkjs"), { recursive: true, force: true });
  await mkdir(VENDOR, { recursive: true });
  await cp(join(SRC_DIR, "deploy", "sdkjs"), join(VENDOR, "sdkjs"), { recursive: true });

  // The sdkjs editor core assumes a few third-party libs are loaded *before*
  // sdk-all-min.js — they are NOT concatenated into the bundle nor copied into
  // deploy/ by build.py (in ONLYOFFICE's own deployment the editor HTML loads
  // them as separate <script> tags). Without them the bundle throws e.g.
  // "XRegExp is not defined" / "jQuery is not defined" mid-execution and never
  // defines Asc.PDFEditorApi. Vendor them from the same pinned sdkjs source.
  console.log("→ vendoring third-party libs (jquery, xregexp, polyfill) ...");
  const VENDOR_LIBS = ["jquery.min.js", "xregexp-all-min.js", "polyfill.js"];
  await mkdir(join(VENDOR, "sdkjs", "vendor"), { recursive: true });
  for (const lib of VENDOR_LIBS) {
    const src = join(SRC_DIR, "vendor", lib);
    if (await exists(src)) {
      await cp(src, join(VENDOR, "sdkjs", "vendor", lib));
      console.log(`  ✓ vendor/${lib}`);
    } else {
      console.warn(`  ⚠ vendor/${lib} not found in source — editor may fail to load`);
    }
  }

  // applyDocumentChanges.js is loaded as a standalone script before the SDK
  // bundles in ONLYOFFICE's own deployment; build.py does not copy it into
  // deploy/, so vendor it explicitly next to the bundles.
  {
    const adcSrc = join(SRC_DIR, "common", "applyDocumentChanges.js");
    if (await exists(adcSrc)) {
      await cp(adcSrc, join(VENDOR, "sdkjs", "common", "applyDocumentChanges.js"));
      console.log("  ✓ common/applyDocumentChanges.js");
    }
  }

  console.log("→ patching engine for standalone-viewer compatibility ...");
  await patchDrawingFile();
  await patchTextShaper();
  await patchFormDesignLabels();
  await patchSaveNoPageClear();

  await writeFile(
    join(VENDOR, "PROVENANCE.json"),
    JSON.stringify(
      { source: "ONLYOFFICE/sdkjs", ref: SDKJS_REF, product: "word", license: "AGPL-3.0-only" },
      null, 2
    )
  );
  console.log("\n✓ Engine built and vendored under vendor/onlyoffice/sdkjs");
}

main().catch((err) => {
  console.error(`\n✗ Build failed: ${err.message}`);
  process.exit(1);
});
