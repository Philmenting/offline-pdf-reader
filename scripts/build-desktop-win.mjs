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
  "common/serviceworker", "common/hash", "common/Charts",
  "common/DocxToHtml",
  // NOTE: common/libfont/engine is intentionally kept — the PDF *editor*
  // (Asc.PDFEditorApi) loads this font-shaping engine at runtime for text
  // editing / FreeText annotations. Trimming it leaves editing broken.
  "common/libfont/test",
  "pdf/build", "pdf/test",
];

const TRIM_FILES = [
  "word/sdk-all.js",
  "pdf/src/engine/drawingfile_ie.js",
  "pdf/src/engine/drawingfile_native.js",
];

// ── TTF parsing ─────────────────────────────────────────────────────────

function readU16BE(b, o) { return (b[o] << 8) | b[o + 1]; }
function readU32BE(b, o) { return ((b[o] << 24) | (b[o+1] << 16) | (b[o+2] << 8) | b[o+3]) >>> 0; }

function parseTTF(buf) {
  if (buf.length < 12) return null;
  const sig = readU32BE(buf, 0);
  if (sig !== 0x00010000 && sig !== 0x74727565 && sig !== 0x4F54544F) return null;
  const numTables = readU16BE(buf, 4);
  const tables = {};
  for (let i = 0; i < numTables; i++) {
    const off = 12 + i * 16;
    const tag = String.fromCharCode(buf[off], buf[off+1], buf[off+2], buf[off+3]);
    tables[tag] = { offset: readU32BE(buf, off + 8), length: readU32BE(buf, off + 12) };
  }
  const nt = tables["name"];
  if (!nt) return null;
  const noff = nt.offset;
  const count = readU16BE(buf, noff + 2);
  const strOff = noff + readU16BE(buf, noff + 4);
  const names = {};
  for (let i = 0; i < count; i++) {
    const r = noff + 6 + i * 12;
    const pid = readU16BE(buf, r), eid = readU16BE(buf, r+2);
    const nid = readU16BE(buf, r+6), len = readU16BE(buf, r+8);
    const so = strOff + readU16BE(buf, r+10);
    if (len === 0) continue;
    let s;
    if (pid === 3 && eid === 1) {
      const c = []; for (let j = 0; j < len; j += 2) c.push(readU16BE(buf, so+j));
      s = String.fromCharCode(...c);
    } else if (pid === 1 && eid === 0) {
      s = ""; for (let j = 0; j < len; j++) s += String.fromCharCode(buf[so+j]);
    } else continue;
    if (!names[nid] || pid === 3) names[nid] = s;
  }
  const os2 = tables["OS/2"];
  let bold = false, italic = false;
  if (os2) {
    const o = os2.offset;
    italic = !!(readU16BE(buf, o+62) & 1);
    bold = !!(readU16BE(buf, o+62) & 32);
    if (!bold && readU16BE(buf, o+4) >= 700) bold = true;
  } else if (tables["head"]) {
    const ms = readU16BE(buf, tables["head"].offset + 44);
    bold = !!(ms & 1); italic = !!(ms & 2);
  }
  const style = bold && italic ? "bolditalic" : bold ? "bold" : italic ? "italic" : "regular";
  const family = names[16] || names[1];
  return family ? { family, style } : null;
}

async function generateSlimAllFonts(fontsDir, outputPath) {
  const files = (await readdir(fontsDir))
    .filter(f => [".ttf", ".otf"].includes(extname(f).toLowerCase())).sort();
  const families = new Map();
  const fileNames = [];
  const fileIdx = new Map();
  for (const f of files) {
    const buf = await readFile(join(fontsDir, f));
    const info = parseTTF(buf);
    if (!info) continue;
    let idx;
    if (fileIdx.has(f)) idx = fileIdx.get(f);
    else { idx = fileNames.length; fileNames.push(f); fileIdx.set(f, idx); }
    if (!families.has(info.family)) families.set(info.family, {});
    const fam = families.get(info.family);
    if (!fam[info.style]) fam[info.style] = { fileIndex: idx, faceIndex: 0 };
  }
  const sorted = [...families.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  const infos = sorted.map(([name, styles]) => {
    const r = styles.regular || styles.bold || styles.italic || styles.bolditalic;
    const i = styles.italic || r;
    const b = styles.bold || r;
    const bi = styles.bolditalic || styles.bold || styles.italic || r;
    return [name, r?.fileIndex??-1, r?.faceIndex??-1, i?.fileIndex??-1, i?.faceIndex??-1,
      b?.fileIndex??-1, b?.faceIndex??-1, bi?.fileIndex??-1, bi?.faceIndex??-1];
  });
  // g_fonts_selection_bin must be "" (not undefined): viewer.js does
  // `"" != g_fonts_selection_bin` and base64-decodes it, throwing on undefined.
  const js = `(function(w) {\nw["__fonts_files"] = ${JSON.stringify(fileNames)};\nw["__fonts_infos"] = ${JSON.stringify(infos)};\nw["g_fonts_selection_bin"] = "";\n})(window);\n`;
  await writeFile(outputPath, js, "utf8");
  return { families: infos.length, files: fileNames.length };
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
  const vendorFonts = join(ROOT, "vendor", "fonts");
  if (!(await exists(vendorEngine))) { console.error("Run: npm run build-engine"); process.exit(1); }
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

  // electron-main.js
  await cp(join(ROOT, "electron-main.js"), join(APP, "electron-main.js"));

  // package.json for electron
  await writeFile(join(APP, "package.json"), JSON.stringify({
    name: "offline-pdf-editor",
    version: "0.1.0",
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
