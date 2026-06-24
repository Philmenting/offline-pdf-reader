#!/usr/bin/env node
/**
 * Generates the font registry (AllFonts.js) and fonts directory required by
 * the ONLYOFFICE PDF engine.
 *
 * Downloads TTF files from the ONLYOFFICE/core-fonts GitHub repository,
 * parses font metadata (name tables, OS/2 tables), groups fonts into
 * families with style variants, and produces:
 *
 *   vendor/onlyoffice/sdkjs/common/AllFonts.js
 *   vendor/fonts/<name>.ttf
 *
 * The generated AllFonts.js populates window["__fonts_files"] (font file
 * paths) and window["__fonts_infos"] (family→style-index mapping) which
 * are consumed by AscFonts.checkAllFonts() in the sdkjs engine.
 *
 * Usage: node scripts/generate-allfonts.mjs
 *
 * License: AGPL-3.0-only (fonts are Apache-2.0 / OFL / GPL per upstream).
 */
import { mkdir, writeFile, readdir, stat, readFile } from "node:fs/promises";
import { join, dirname, basename, extname } from "node:path";
import { fileURLToPath } from "node:url";
import { createWriteStream } from "node:fs";
import { Readable } from "node:stream";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");
const FONTS_DIR = join(ROOT, "vendor", "fonts");
const ALLFONTS_PATH = join(ROOT, "vendor", "onlyoffice", "sdkjs", "common", "AllFonts.js");

const CORE_FONTS_REF = "master";
const CORE_FONTS_TARBALL = `https://codeload.github.com/ONLYOFFICE/core-fonts/tar.gz/${CORE_FONTS_REF}`;
const CACHE_DIR = join(ROOT, ".build");
const TAR_PATH = join(CACHE_DIR, "core-fonts.tar.gz");
const EXTRACT_DIR = join(CACHE_DIR, "core-fonts");

// ── TTF parsing helpers ─────────────────────────────────────────────────

function readU16BE(buf, off) { return (buf[off] << 8) | buf[off + 1]; }
function readI16BE(buf, off) { const v = readU16BE(buf, off); return v > 32767 ? v - 65536 : v; }
function readU32BE(buf, off) { return ((buf[off] << 24) | (buf[off+1] << 16) | (buf[off+2] << 8) | buf[off+3]) >>> 0; }

function parseTTFTables(buf) {
  if (buf.length < 12) return null;
  const sig = readU32BE(buf, 0);
  // TrueType: 0x00010000 or 'true'; OpenType: 'OTTO'
  if (sig !== 0x00010000 && sig !== 0x74727565 && sig !== 0x4F54544F) return null;

  const numTables = readU16BE(buf, 4);
  const tables = {};
  for (let i = 0; i < numTables; i++) {
    const off = 12 + i * 16;
    const tag = String.fromCharCode(buf[off], buf[off+1], buf[off+2], buf[off+3]);
    tables[tag] = { offset: readU32BE(buf, off + 8), length: readU32BE(buf, off + 12) };
  }
  return tables;
}

function parseNameTable(buf, tables) {
  const t = tables["name"];
  if (!t) return {};
  const off = t.offset;
  const format = readU16BE(buf, off);
  const count = readU16BE(buf, off + 2);
  const stringOffset = off + readU16BE(buf, off + 4);

  const names = {};
  for (let i = 0; i < count; i++) {
    const recOff = off + 6 + i * 12;
    const platformID = readU16BE(buf, recOff);
    const encodingID = readU16BE(buf, recOff + 2);
    const nameID = readU16BE(buf, recOff + 6);
    const length = readU16BE(buf, recOff + 8);
    const strOff = stringOffset + readU16BE(buf, recOff + 10);

    if (length === 0) continue;
    // Prefer Windows Unicode BMP (3,1) or Mac Roman (1,0)
    let str;
    if (platformID === 3 && encodingID === 1) {
      const chars = [];
      for (let j = 0; j < length; j += 2) {
        chars.push(readU16BE(buf, strOff + j));
      }
      str = String.fromCharCode(...chars);
    } else if (platformID === 1 && encodingID === 0) {
      str = "";
      for (let j = 0; j < length; j++) {
        str += String.fromCharCode(buf[strOff + j]);
      }
    } else {
      continue;
    }

    // Prefer platform 3 over platform 1
    if (!names[nameID] || platformID === 3) {
      names[nameID] = str;
    }
  }
  return names;
}

function parseOS2Table(buf, tables) {
  const t = tables["OS/2"];
  if (!t) return null;
  const off = t.offset;
  return {
    usWeightClass: readU16BE(buf, off + 4),
    fsSelection: readU16BE(buf, off + 62),
  };
}

function parseHeadTable(buf, tables) {
  const t = tables["head"];
  if (!t) return null;
  const off = t.offset;
  return {
    macStyle: readU16BE(buf, off + 44),
  };
}

// Determine font style from OS/2 and head tables
function getFontStyle(os2, head) {
  let bold = false, italic = false;

  if (os2) {
    // fsSelection: bit 0 = italic, bit 5 = bold
    italic = !!(os2.fsSelection & 1);
    bold = !!(os2.fsSelection & 32);
    // Also check weight class
    if (!bold && os2.usWeightClass >= 700) bold = true;
  } else if (head) {
    // macStyle: bit 0 = bold, bit 1 = italic
    bold = !!(head.macStyle & 1);
    italic = !!(head.macStyle & 2);
  }

  if (bold && italic) return "bolditalic";
  if (bold) return "bold";
  if (italic) return "italic";
  return "regular";
}

function parseFontFile(buf) {
  const tables = parseTTFTables(buf);
  if (!tables) return null;

  const names = parseNameTable(buf, tables);
  const os2 = parseOS2Table(buf, tables);
  const head = parseHeadTable(buf, tables);

  // nameID 1 = font family, nameID 4 = full name, nameID 16 = typographic family
  const family = names[16] || names[1];
  if (!family) return null;

  const style = getFontStyle(os2, head);
  const fullName = names[4] || family;

  return { family, style, fullName };
}

// ── TTC (TrueType Collection) support ───────────────────────────────────

function parseTTC(buf) {
  const tag = String.fromCharCode(buf[0], buf[1], buf[2], buf[3]);
  if (tag !== "ttcf") return null;
  const numFonts = readU32BE(buf, 8);
  const fonts = [];
  for (let i = 0; i < numFonts; i++) {
    const offset = readU32BE(buf, 12 + i * 4);
    // Parse tables starting from offset
    const numTables = readU16BE(buf, offset + 4);
    const tables = {};
    for (let j = 0; j < numTables; j++) {
      const tOff = offset + 12 + j * 16;
      const ttag = String.fromCharCode(buf[tOff], buf[tOff+1], buf[tOff+2], buf[tOff+3]);
      tables[ttag] = { offset: readU32BE(buf, tOff + 8), length: readU32BE(buf, tOff + 12) };
    }
    const names = parseNameTable(buf, tables);
    const os2 = parseOS2Table(buf, tables);
    const head = parseHeadTable(buf, tables);
    const family = names[16] || names[1];
    if (!family) continue;
    const style = getFontStyle(os2, head);
    fonts.push({ family, style, fullName: names[4] || family, faceIndex: i });
  }
  return fonts;
}

// ── Download & extract ──────────────────────────────────────────────────

async function exists(p) {
  try { await stat(p); return true; } catch { return false; }
}

async function download(url, dest) {
  console.log(`  Downloading ${url.slice(0, 80)}…`);
  const res = await fetch(url, { redirect: "follow" });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  const ws = createWriteStream(dest);
  await new Promise((resolve, reject) => {
    Readable.fromWeb(res.body).pipe(ws).on("finish", resolve).on("error", reject);
  });
}

async function extractTarGz(tarPath, dest) {
  const { spawn } = await import("node:child_process");
  await mkdir(dest, { recursive: true });
  return new Promise((resolve, reject) => {
    const p = spawn("tar", ["xzf", tarPath, "--strip-components=1", "-C", dest], { stdio: "inherit" });
    p.on("error", reject);
    p.on("close", (code) => code === 0 ? resolve() : reject(new Error(`tar exited ${code}`)));
  });
}

// ── Collect all TTF/TTC files recursively ───────────────────────────────

async function collectFontFiles(dir) {
  const results = [];
  const entries = await readdir(dir, { withFileTypes: true });
  for (const e of entries) {
    const full = join(dir, e.name);
    if (e.isDirectory()) {
      results.push(...await collectFontFiles(full));
    } else {
      const ext = extname(e.name).toLowerCase();
      if (ext === ".ttf" || ext === ".ttc" || ext === ".otf") {
        results.push(full);
      }
    }
  }
  return results;
}

// ── Main ────────────────────────────────────────────────────────────────

async function main() {
  console.log("=== AllFonts.js Generator ===\n");

  // 1. Download core-fonts if not cached
  await mkdir(CACHE_DIR, { recursive: true });

  if (!(await exists(EXTRACT_DIR))) {
    if (!(await exists(TAR_PATH))) {
      await download(CORE_FONTS_TARBALL, TAR_PATH);
    }
    console.log("  Extracting core-fonts…");
    await extractTarGz(TAR_PATH, EXTRACT_DIR);
  } else {
    console.log("  Using cached core-fonts.");
  }

  // 2. Collect all font files
  const fontPaths = await collectFontFiles(EXTRACT_DIR);
  console.log(`  Found ${fontPaths.length} font files.`);

  // 3. Parse each font and group by family
  // families: Map<familyName, { regular, italic, bold, bolditalic }>
  // Each value stores { filePath, faceIndex }
  const families = new Map();
  // filesList: array of unique font file paths (for __fonts_files)
  const filesList = [];
  const fileIndexMap = new Map(); // filePath -> index in filesList

  function getFileIndex(filePath) {
    if (fileIndexMap.has(filePath)) return fileIndexMap.get(filePath);
    const idx = filesList.length;
    filesList.push(filePath);
    fileIndexMap.set(filePath, idx);
    return idx;
  }

  for (const fp of fontPaths) {
    const buf = await readFile(fp);
    const ext = extname(fp).toLowerCase();

    if (ext === ".ttc") {
      const ttcFonts = parseTTC(buf);
      if (!ttcFonts) continue;
      for (const font of ttcFonts) {
        const fileIdx = getFileIndex(fp);
        if (!families.has(font.family)) {
          families.set(font.family, {});
        }
        const fam = families.get(font.family);
        if (!fam[font.style]) {
          fam[font.style] = { fileIndex: fileIdx, faceIndex: font.faceIndex };
        }
      }
    } else {
      const info = parseFontFile(buf);
      if (!info) continue;
      const fileIdx = getFileIndex(fp);
      if (!families.has(info.family)) {
        families.set(info.family, {});
      }
      const fam = families.get(info.family);
      if (!fam[info.style]) {
        fam[info.style] = { fileIndex: fileIdx, faceIndex: 0 };
      }
    }
  }

  console.log(`  Parsed ${families.size} font families.`);

  // 4. Copy font files to vendor/fonts/ and build the file list
  await mkdir(FONTS_DIR, { recursive: true });
  const finalFileNames = []; // index-aligned with filesList

  for (const fp of filesList) {
    const name = basename(fp);
    const dest = join(FONTS_DIR, name);
    await writeFile(dest, await readFile(fp));
    finalFileNames.push(name);
  }

  console.log(`  Copied ${finalFileNames.length} font files to vendor/fonts/.`);

  // 5. Generate AllFonts.js
  // __fonts_files: array of filename strings
  // __fonts_infos: array of [familyName, idxR, faceR, idxI, faceI, idxB, faceB, idxBI, faceBI]
  const sortedFamilies = [...families.entries()].sort((a, b) => a[0].localeCompare(b[0]));

  const infos = [];
  for (const [name, styles] of sortedFamilies) {
    const r = styles.regular || styles.bold || styles.italic || styles.bolditalic;
    const i = styles.italic || r;
    const b = styles.bold || r;
    const bi = styles.bolditalic || styles.bold || styles.italic || r;

    infos.push([
      name,
      r ? r.fileIndex : -1, r ? r.faceIndex : -1,
      i ? i.fileIndex : -1, i ? i.faceIndex : -1,
      b ? b.fileIndex : -1, b ? b.faceIndex : -1,
      bi ? bi.fileIndex : -1, bi ? bi.faceIndex : -1,
    ]);
  }

  const jsContent = `// Generated by generate-allfonts.mjs — do not edit.
// Font data from ONLYOFFICE/core-fonts (Apache-2.0 / OFL / GPL).
(function(w) {
w["__fonts_files"] = ${JSON.stringify(finalFileNames)};
w["__fonts_infos"] = ${JSON.stringify(infos)};
// The engine's viewer.js checks "" != g_fonts_selection_bin and tries to
// base64-decode it; if left undefined that decode throws and no page renders.
// We ship no precomputed font-selection table, so set "" for runtime fallback.
w["g_fonts_selection_bin"] = "";
})(window);
`;

  await mkdir(dirname(ALLFONTS_PATH), { recursive: true });
  await writeFile(ALLFONTS_PATH, jsContent, "utf8");

  console.log(`\n  Generated AllFonts.js with ${infos.length} families and ${finalFileNames.length} files.`);
  console.log(`  → ${ALLFONTS_PATH}`);
  console.log(`  → ${FONTS_DIR}/`);
  console.log("\nDone!");
}

main().catch((err) => {
  console.error("Error:", err);
  process.exit(1);
});
