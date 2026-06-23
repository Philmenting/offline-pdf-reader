#!/usr/bin/env node
/**
 * Builds a portable Windows ZIP of the Offline PDF Editor.
 *
 * The package includes:
 *   - A portable Node.js runtime (node.exe)
 *   - The static file server (server.mjs)
 *   - The host app (public/)
 *   - The ONLYOFFICE viewer engine + Western fonts (vendor/)
 *   - A batch launcher (Starten.bat)
 *
 * Prerequisites: run `npm run build-engine` and `npm run generate-fonts` first.
 *
 * Usage: node scripts/build-portable-win.mjs
 */
import { mkdir, rm, writeFile, readFile, readdir, stat, cp } from "node:fs/promises";
import { join, dirname, extname, basename } from "node:path";
import { fileURLToPath } from "node:url";
import { createWriteStream } from "node:fs";
import { Readable } from "node:stream";
import { spawn } from "node:child_process";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");
const DIST = join(ROOT, "dist", "Offline-PDF-Editor");
const ZIP_OUT = join(ROOT, "dist", "Offline-PDF-Editor.zip");

const NODE_VER = "v22.22.2";
const NODE_ZIP_NAME = `node-${NODE_VER}-win-x64.zip`;
const NODE_URL = `https://nodejs.org/dist/${NODE_VER}/${NODE_ZIP_NAME}`;
const CACHE = join(ROOT, ".build");

const WESTERN_FONT_PREFIXES = [
  "ASC", "Liberation", "DejaVu", "Free", "OpenSans", "opens___",
  "Ubuntu", "Carlito", "caladea", "Symbola",
];

const REMOVE_DIRS = [
  "common/SmartArts", "common/spell", "common/Native",
  "common/serviceworker", "common/hash", "common/Charts",
  "common/Drawings", "common/DocxToHtml",
  "common/libfont/engine", "common/libfont/test",
  "pdf/build", "pdf/test",
];

const REMOVE_FILES = [
  "word/sdk-all.js",
  "pdf/src/engine/drawingfile_ie.js",
  "pdf/src/engine/drawingfile_native.js",
];

// ── TTF parsing (same as generate-allfonts.mjs) ────────────────────────

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
    if (fileIdx.has(f)) { idx = fileIdx.get(f); }
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

  const js = `// Generated AllFonts.js (slim Western font set)\n(function(w) {\nw["__fonts_files"] = ${JSON.stringify(fileNames)};\nw["__fonts_infos"] = ${JSON.stringify(infos)};\n})(window);\n`;
  await writeFile(outputPath, js, "utf8");
  return { families: infos.length, files: fileNames.length };
}

// ── Main ────────────────────────────────────────────────────────────────

async function main() {
  console.log("=== Building Portable Windows Package ===\n");

  // Verify prerequisites
  const vendorEngine = join(ROOT, "vendor", "onlyoffice", "sdkjs", "pdf", "src", "engine", "viewer.js");
  const vendorFonts = join(ROOT, "vendor", "fonts");
  if (!(await exists(vendorEngine))) {
    console.error("Engine not built. Run: npm run build-engine");
    process.exit(1);
  }
  if (!(await exists(vendorFonts))) {
    console.error("Fonts not generated. Run: npm run generate-fonts");
    process.exit(1);
  }

  // Clean
  await rm(DIST, { recursive: true, force: true });
  await rm(ZIP_OUT, { force: true });

  // 1. Download portable Node.js for Windows
  await mkdir(CACHE, { recursive: true });
  const nodeZipPath = join(CACHE, NODE_ZIP_NAME);
  if (!(await exists(nodeZipPath))) {
    await download(NODE_URL, nodeZipPath);
  }
  console.log("  Extracting node.exe…");
  await mkdir(join(DIST, "runtime"), { recursive: true });
  await run("unzip", ["-jo", nodeZipPath, `node-${NODE_VER}-win-x64/node.exe`, "-d", join(DIST, "runtime")]);

  // 2. Copy app files
  console.log("  Copying app files…");
  await cp(join(ROOT, "server.mjs"), join(DIST, "server.mjs"));
  await cp(join(ROOT, "public"), join(DIST, "public"), { recursive: true });
  await cp(join(ROOT, "vendor", "onlyoffice"), join(DIST, "vendor", "onlyoffice"), { recursive: true });

  // 3. Copy only Western fonts
  console.log("  Copying Western fonts…");
  await mkdir(join(DIST, "vendor", "fonts"), { recursive: true });
  const allFonts = await readdir(vendorFonts);
  let fontCount = 0;
  for (const f of allFonts) {
    if (WESTERN_FONT_PREFIXES.some(p => f.startsWith(p))) {
      await cp(join(vendorFonts, f), join(DIST, "vendor", "fonts", f));
      fontCount++;
    }
  }
  console.log(`  Copied ${fontCount} font files.`);

  // 4. Regenerate AllFonts.js for slim font set
  console.log("  Generating slim AllFonts.js…");
  const result = await generateSlimAllFonts(
    join(DIST, "vendor", "fonts"),
    join(DIST, "vendor", "onlyoffice", "sdkjs", "common", "AllFonts.js")
  );
  console.log(`  ${result.families} families, ${result.files} files.`);

  // 5. Remove unnecessary engine files
  console.log("  Trimming engine…");
  const sdkjs = join(DIST, "vendor", "onlyoffice", "sdkjs");
  for (const d of REMOVE_DIRS) {
    await rm(join(sdkjs, d), { recursive: true, force: true });
  }
  for (const f of REMOVE_FILES) {
    await rm(join(sdkjs, f), { force: true });
  }
  // Remove pdf/src subdirectories except engine/
  const pdfSrc = join(sdkjs, "pdf", "src");
  if (await exists(pdfSrc)) {
    for (const e of await readdir(pdfSrc, { withFileTypes: true })) {
      if (e.isFile()) await rm(join(pdfSrc, e.name), { force: true });
      else if (e.name !== "engine") await rm(join(pdfSrc, e.name), { recursive: true, force: true });
    }
  }

  // 6. Write batch launcher
  const bat = `@echo off\r\ntitle Offline PDF Editor\r\necho ============================================\r\necho   Offline PDF Editor wird gestartet...\r\necho ============================================\r\necho.\r\n\r\nset "DIR=%~dp0"\r\nset "NODE=%DIR%runtime\\node.exe"\r\n\r\nif not exist "%NODE%" (\r\n    echo FEHLER: node.exe nicht gefunden.\r\n    echo Bitte entpacke das gesamte ZIP-Archiv.\r\n    pause\r\n    exit /b 1\r\n)\r\n\r\necho Server startet auf http://localhost:3000\r\necho Browser wird geoeffnet...\r\necho.\r\necho Zum Beenden dieses Fenster schliessen oder Ctrl+C druecken.\r\necho.\r\n\r\nstart "" "http://localhost:3000"\r\n"%NODE%" "%DIR%server.mjs"\r\n`;
  await writeFile(join(DIST, "Starten.bat"), bat);

  // 7. Create ZIP
  console.log("  Creating ZIP…");
  await run("zip", ["-r", "-q", ZIP_OUT, "Offline-PDF-Editor"], { cwd: join(ROOT, "dist") });

  const zipStat = await stat(ZIP_OUT);
  const sizeMB = (zipStat.size / 1024 / 1024).toFixed(1);
  console.log(`\n  Package: dist/Offline-PDF-Editor.zip (${sizeMB} MB)`);
  console.log("  Zum Testen: ZIP entpacken → Starten.bat doppelklicken.");
  console.log("\nDone!");
}

main().catch((err) => { console.error("Error:", err); process.exit(1); });
