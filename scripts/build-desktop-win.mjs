#!/usr/bin/env node
/**
 * Builds a standalone Windows desktop app (Electron-based, no browser needed).
 *
 * Downloads the Windows Electron binary, bundles it with the PDF editor app
 * (the ONLYOFFICE PDF *editor* engine incl. the font-shaping engine, the word
 * sdk-all-min.js editor bundle, Western fonts, host UI), and produces a
 * ready-to-run ZIP.
 *
 * Prerequisites: run `npm run build-engine` and `npm run generate-fonts` first.
 *
 * Usage: node scripts/build-desktop-win.mjs
 */
import { mkdir, rm, cp, readdir, readFile, writeFile, stat } from "node:fs/promises";
import { join, dirname, extname, basename } from "node:path";
import { fileURLToPath } from "node:url";
import { createWriteStream } from "node:fs";
import { Readable } from "node:stream";
import { spawn } from "node:child_process";
import { buildRegistry, renderAllFontsJs, odttfToggle } from "./fonts-lib.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");
const CACHE = join(ROOT, ".build");
const DIST = join(ROOT, "dist", "Offline-PDF-Editor");
const ZIP_OUT = join(ROOT, "dist", "Offline-PDF-Editor.zip");

const ELECTRON_VER = "v42.5.0";
const ELECTRON_ZIP = `electron-${ELECTRON_VER}-win32-x64.zip`;
const ELECTRON_URL = `https://github.com/electron/electron/releases/download/${ELECTRON_VER}/${ELECTRON_ZIP}`;

const WESTERN_FONT_PREFIXES = [
  "ASC", "Liberation", "DejaVu", "Free", "OpenSans", "opens___",
  "Ubuntu", "Carlito", "caladea", "Symbola",
];

const TRIM_DIRS = [
  "common/SmartArts", "common/spell", "common/Native",
  "common/serviceworker", "common/hash",
  "common/DocxToHtml",
  // NOTE: common/libfont/engine is intentionally kept — the PDF *editor*
  // (Asc.PDFEditorApi) loads this font-shaping engine at runtime for text
  // editing / FreeText annotations. Trimming it leaves editing broken.
  // common/Charts is also kept — the editor eagerly loads Charts/ChartStyles.js
  // during init; trimming it produced a LoadingScriptError (asc_onError -24).
  "common/libfont/test",
  "pdf/build", "pdf/test",
];

const TRIM_FILES = [
  // NOTE: word/sdk-all.js must NOT be trimmed — it is the second half of the
  // editor (History, document model, annotations, drawings); without it the
  // editor throws "Cannot read properties of undefined (reading 'Add')" and
  // falls back to read-only.
  "pdf/src/engine/drawingfile_ie.js",
  "pdf/src/engine/drawingfile_native.js",
];

// ── Slim AllFonts.js (full-fidelity registry over the Western subset) ───
//
// Uses the same machinery as generate-allfonts.mjs (scripts/fonts-lib.mjs),
// so the desktop package ships a REAL g_fonts_selection_bin. Shipping an
// empty one would reintroduce the typed-text artifact bug: with no selection
// records every font name resolves to the engine's built-in ASCW3 dummy font
// and typed characters render as .notdef boxes.
//
// The files in vendor/fonts/ are stored odttf-obfuscated (the web font
// loader XOR-decodes every fetched font), so de-obfuscate before parsing.
async function generateSlimAllFonts(fontsDir, outputPath) {
  const files = (await readdir(fontsDir))
    .filter(f => [".ttf", ".otf", ".ttc"].includes(extname(f).toLowerCase())).sort();
  const fonts = [];
  for (const f of files) {
    fonts.push({
      name: f,
      data: odttfToggle(await readFile(join(fontsDir, f))),
      ext: extname(f).toLowerCase(),
    });
  }
  const registry = buildRegistry(fonts);
  await writeFile(outputPath, renderAllFontsJs(registry), "utf8");
  return { families: registry.infos.length, files: registry.fileNames.length, records: registry.selectionFaces.length };
}

// ── Helpers ─────────────────────────────────────────────────────────────

async function exists(p) { try { await stat(p); return true; } catch { return false; } }

async function download(url, dest) {
  console.log(`  Downloading ${basename(dest)}…`);
  const res = await fetch(url, { redirect: "follow" });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  const ws = createWriteStream(dest);
  await new Promise((resolve, reject) => {
    Readable.fromWeb(res.body).pipe(ws).on("finish", resolve).on("error", reject);
  });
}

function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, args, { stdio: "inherit", ...opts });
    p.on("error", reject);
    p.on("close", (code) => code === 0 ? resolve() : reject(new Error(`${cmd} exited ${code}`)));
  });
}

// ── Main ────────────────────────────────────────────────────────────────

async function main() {
  console.log("=== Building Offline PDF Editor (Windows Desktop) ===\n");

  const vendorEngine = join(ROOT, "vendor", "onlyoffice", "sdkjs", "pdf", "src", "engine", "viewer.js");
  const sdkjsWord = join(ROOT, "vendor", "onlyoffice", "sdkjs", "word");
  const sdkjsVendor = join(ROOT, "vendor", "onlyoffice", "sdkjs", "vendor");
  const vendorFonts = join(ROOT, "vendor", "fonts");
  if (!(await exists(vendorEngine))) { console.error("Run: npm run build-engine"); process.exit(1); }
  for (const bundle of ["sdk-all-min.js", "sdk-all.js"]) {
    if (!(await exists(join(sdkjsWord, bundle)))) {
      console.error(`Editor bundle word/${bundle} missing — run: npm run build-engine`);
      process.exit(1);
    }
  }
  for (const lib of ["jquery.min.js", "xregexp-all-min.js"]) {
    if (!(await exists(join(sdkjsVendor, lib)))) {
      console.error(`Editor dependency vendor/${lib} missing — re-run: npm run build-engine`);
      process.exit(1);
    }
  }
  if (!(await exists(vendorFonts))) { console.error("Run: npm run generate-fonts"); process.exit(1); }

  // 1. Download Windows Electron
  await mkdir(CACHE, { recursive: true });
  const electronZip = join(CACHE, ELECTRON_ZIP);
  if (!(await exists(electronZip))) {
    await download(ELECTRON_URL, electronZip);
  } else {
    console.log("  Using cached Electron Windows binary.");
  }

  // 2. Extract Electron
  console.log("  Extracting Electron…");
  await rm(DIST, { recursive: true, force: true });
  await mkdir(DIST, { recursive: true });
  await run("unzip", ["-q", electronZip, "-d", DIST]);

  // Rename executable
  const { rename } = await import("node:fs/promises");
  await rename(join(DIST, "electron.exe"), join(DIST, "Offline-PDF-Editor.exe"));
  await rm(join(DIST, "resources", "default_app.asar"), { force: true });

  // 3. Create app directory
  console.log("  Bundling app…");
  const APP = join(DIST, "resources", "app");
  await mkdir(APP, { recursive: true });

  // electron-main.js + preload bridge (native save dialog)
  await cp(join(ROOT, "electron-main.js"), join(APP, "electron-main.js"));
  await cp(join(ROOT, "preload.js"), join(APP, "preload.js"));

  // package.json for electron (version mirrors the repo's package.json)
  const rootVersion = JSON.parse(await readFile(join(ROOT, "package.json"), "utf8")).version;
  await writeFile(join(APP, "package.json"), JSON.stringify({
    name: "offline-pdf-editor",
    version: rootVersion,
    main: "electron-main.js",
  }, null, 2));

  // public/
  await cp(join(ROOT, "public"), join(APP, "public"), { recursive: true });

  // vendor/onlyoffice (engine)
  await cp(join(ROOT, "vendor", "onlyoffice"), join(APP, "vendor", "onlyoffice"), { recursive: true });

  // vendor/fonts (Western only)
  await mkdir(join(APP, "vendor", "fonts"), { recursive: true });
  const allFontFiles = await readdir(vendorFonts);
  let fontCount = 0;
  for (const f of allFontFiles) {
    if (WESTERN_FONT_PREFIXES.some(p => f.startsWith(p))) {
      await cp(join(vendorFonts, f), join(APP, "vendor", "fonts", f));
      fontCount++;
    }
  }
  console.log(`  ${fontCount} Western font files copied.`);

  // 4. Regenerate AllFonts.js for slim font set
  const r = await generateSlimAllFonts(
    join(APP, "vendor", "fonts"),
    join(APP, "vendor", "onlyoffice", "sdkjs", "common", "AllFonts.js")
  );
  console.log(`  AllFonts.js: ${r.families} families, ${r.files} files.`);

  // 5. Trim engine
  console.log("  Trimming engine…");
  const sdkjs = join(APP, "vendor", "onlyoffice", "sdkjs");
  for (const d of TRIM_DIRS) await rm(join(sdkjs, d), { recursive: true, force: true });
  for (const f of TRIM_FILES) await rm(join(sdkjs, f), { force: true });
  const pdfSrc = join(sdkjs, "pdf", "src");
  if (await exists(pdfSrc)) {
    for (const e of await readdir(pdfSrc, { withFileTypes: true })) {
      if (e.isFile()) await rm(join(pdfSrc, e.name));
      else if (e.name !== "engine") await rm(join(pdfSrc, e.name), { recursive: true });
    }
  }

  // 6. Create ZIP
  console.log("  Creating ZIP…");
  await rm(ZIP_OUT, { force: true });
  await run("zip", ["-r", "-q", ZIP_OUT, basename(DIST)], { cwd: dirname(DIST) });

  const zipStat = await stat(ZIP_OUT);
  const sizeMB = (zipStat.size / 1024 / 1024).toFixed(0);
  console.log(`\n  ✓ dist/Offline-PDF-Editor.zip (${sizeMB} MB)`);
  console.log("  Zum Testen: ZIP entpacken → Offline-PDF-Editor.exe starten.");
  console.log("\nDone!");
}

main().catch((err) => { console.error("Error:", err); process.exit(1); });
