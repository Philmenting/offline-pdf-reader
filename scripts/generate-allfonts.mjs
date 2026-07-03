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
import { buildFontRanges } from "./font-ranges.mjs";

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
  const len = t.length;
  const has = (rel, size) => rel + size <= len && off + rel + size <= buf.length;

  const os2 = {
    version: readU16BE(buf, off),
    xAvgCharWidth: readI16BE(buf, off + 2),
    usWeightClass: readU16BE(buf, off + 4),
    usWidthClass: readU16BE(buf, off + 6),
    fsType: readU16BE(buf, off + 8),
    sFamilyClass: readI16BE(buf, off + 30),
    panose: Array.from(buf.subarray(off + 32, off + 42)),
    ulUnicodeRange1: readU32BE(buf, off + 42),
    ulUnicodeRange2: readU32BE(buf, off + 46),
    ulUnicodeRange3: readU32BE(buf, off + 50),
    ulUnicodeRange4: readU32BE(buf, off + 54),
    fsSelection: readU16BE(buf, off + 62),
    sTypoAscender: readI16BE(buf, off + 68),
    sTypoDescender: readI16BE(buf, off + 70),
    sTypoLineGap: readI16BE(buf, off + 72),
    ulCodePageRange1: 0,
    ulCodePageRange2: 0,
    sxHeight: 0,
    sCapHeight: 0,
  };
  if (os2.version >= 1 && has(78, 8)) {
    os2.ulCodePageRange1 = readU32BE(buf, off + 78);
    os2.ulCodePageRange2 = readU32BE(buf, off + 82);
  }
  if (os2.version >= 2 && has(86, 4)) {
    os2.sxHeight = readI16BE(buf, off + 86);
    os2.sCapHeight = readI16BE(buf, off + 88);
  }
  return os2;
}

function parsePostTable(buf, tables) {
  const t = tables["post"];
  if (!t || t.length < 16) return null;
  return { isFixedPitch: readU32BE(buf, t.offset + 12) !== 0 };
}

function parseHeadTable(buf, tables) {
  const t = tables["head"];
  if (!t) return null;
  const off = t.offset;
  return {
    unitsPerEm: readU16BE(buf, off + 18),
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

// Font-selection metadata for one face: everything CFontSelect/CFontInfo
// (sdkjs common/libfont/map.js fromStream v2, core ApplicationFonts.cpp
// FromBuffer) scores fonts by. Missing tables degrade to zeros — a zeroed
// field simply contributes no penalty discrimination.
function buildSelectInfo(buf, tables, family, style) {
  const os2 = parseOS2Table(buf, tables);
  const head = parseHeadTable(buf, tables);
  const post = parsePostTable(buf, tables);

  let bold = style === "bold" || style === "bolditalic";
  let italic = style === "italic" || style === "bolditalic";

  // The engine's font scorer compares candidate metrics against dictionary
  // records that are normalised to a 1000-unit em (e.g. Arial's dictionary
  // entry: xAvgCharWidth 441 = 904/2048*1000). Raw font-unit values from
  // 2048-upem fonts would add huge bogus penalties, so normalise the same way.
  const upem = (head && head.unitsPerEm) || 1000;
  const em1000 = (v) => Math.round((v * 1000) / upem);

  return {
    name: family,
    italic,
    bold,
    fixed: post ? post.isFixedPitch : false,
    panose: os2 ? os2.panose : new Array(10).fill(0),
    ulUnicodeRange1: os2 ? os2.ulUnicodeRange1 : 0,
    ulUnicodeRange2: os2 ? os2.ulUnicodeRange2 : 0,
    ulUnicodeRange3: os2 ? os2.ulUnicodeRange3 : 0,
    ulUnicodeRange4: os2 ? os2.ulUnicodeRange4 : 0,
    ulCodePageRange1: os2 ? os2.ulCodePageRange1 : 0,
    ulCodePageRange2: os2 ? os2.ulCodePageRange2 : 0,
    usWeight: os2 ? os2.usWeightClass : (bold ? 700 : 400),
    usWidth: os2 ? os2.usWidthClass : 5,
    sFamilyClass: os2 ? os2.sFamilyClass : 0,
    fontFormat: 1, // EFontFormat::fontTrueType
    shAvgCharWidth: os2 ? em1000(os2.xAvgCharWidth) : 0,
    shAscent: os2 ? em1000(os2.sTypoAscender) : 0,
    shDescent: os2 ? em1000(os2.sTypoDescender) : 0,
    shLineGap: os2 ? em1000(os2.sTypoLineGap) : 0,
    shXHeight: os2 ? em1000(os2.sxHeight) : 0,
    shCapHeight: os2 ? em1000(os2.sCapHeight) : 0,
    usType: os2 ? os2.fsType : 0,
  };
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

  return { family, style, fullName, select: buildSelectInfo(buf, tables, family, style) };
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
    fonts.push({
      family, style, fullName: names[4] || family, faceIndex: i,
      select: buildSelectInfo(buf, tables, family, style),
    });
  }
  return fonts;
}

// ── g_fonts_selection_bin serializer ────────────────────────────────────
//
// The engine's ENTIRE font-name resolution (g_fontApplication.GetFontFileWeb →
// FD_FontDictionary.GetFontIndex → CFontSelect.GetPenalty scoring) runs over
// CFontSelectList.List, which is populated EXCLUSIVELY from this binary. With
// an empty bin the list contains only the engine's built-in "ASCW3" dummy
// (a ~10-glyph checkbox mini-font), so EVERY font name — including our own
// bundled families like "DejaVu Serif" — resolves to ASCW3 and text shaped
// with it renders as .notdef boxes. That was the root cause of the artifact
// boxes appearing when typing in the PDF editor.
//
// Format: the "font_selection.bin" v2 record layout shared by the JS side
// (sdkjs common/libfont/map.js, CFontSelect.fromStream with
// __all_fonts_js_version__ = 2) and the native/WASM side (core
// DesktopEditor/fontengine/ApplicationFonts.cpp, NSFonts::FromBuffer, consumed
// through drawingfile.js _InitializeFontsBase64). All integers little-endian:
//
//   int32 count
//   per record:
//     int32  recordLen                  (includes these 4 bytes)
//     int32  nameLen,  utf8 name        (family name; MUST exist in
//                                        __fonts_infos → g_map_font_index)
//     int32  namesCount (0)
//     int32  pathLen,  utf8 path        (basename; loader joins with fontsPath)
//     int32  faceIndex
//     int32  italic, int32 bold, int32 fixedPitch
//     int32  panoseLen (10), 10 bytes panose
//     uint32 ulUnicodeRange1..4, uint32 ulCodePageRange1..2
//     uint16 usWeight, uint16 usWidth
//     int16  sFamilyClass, int16 fontFormat
//     int16  avgCharWidth, ascent, descent, lineGap, xHeight, capHeight
//     uint16 usType
function buildSelectionBin(faces) {
  const chunks = [];
  const head = Buffer.alloc(4);
  head.writeInt32LE(faces.length, 0);
  chunks.push(head);

  for (const f of faces) {
    const name = Buffer.from(f.select.name, "utf8");
    const path = Buffer.from(f.path, "utf8");
    const recordLen =
      4 +                    // recordLen itself
      4 + name.length +      // name
      4 +                    // namesCount (0)
      4 + path.length +      // path
      4 * 4 +                // faceIndex, italic, bold, fixed
      4 + 10 +               // panoseLen + panose
      6 * 4 +                // unicode ranges + codepage ranges
      2 * 2 +                // weight, width
      2 * 2 +                // familyClass, fontFormat
      6 * 2 +                // metrics
      2;                     // usType
    const b = Buffer.alloc(recordLen);
    let o = 0;
    o = b.writeInt32LE(recordLen, o);
    o = b.writeInt32LE(name.length, o); o += name.copy(b, o);
    o = b.writeInt32LE(0, o);
    o = b.writeInt32LE(path.length, o); o += path.copy(b, o);
    o = b.writeInt32LE(f.faceIndex, o);
    o = b.writeInt32LE(f.select.italic ? 1 : 0, o);
    o = b.writeInt32LE(f.select.bold ? 1 : 0, o);
    o = b.writeInt32LE(f.select.fixed ? 1 : 0, o);
    o = b.writeInt32LE(10, o);
    for (let i = 0; i < 10; i++) o = b.writeUInt8(f.select.panose[i] & 0xFF, o);
    o = b.writeUInt32LE(f.select.ulUnicodeRange1 >>> 0, o);
    o = b.writeUInt32LE(f.select.ulUnicodeRange2 >>> 0, o);
    o = b.writeUInt32LE(f.select.ulUnicodeRange3 >>> 0, o);
    o = b.writeUInt32LE(f.select.ulUnicodeRange4 >>> 0, o);
    o = b.writeUInt32LE(f.select.ulCodePageRange1 >>> 0, o);
    o = b.writeUInt32LE(f.select.ulCodePageRange2 >>> 0, o);
    o = b.writeUInt16LE(f.select.usWeight & 0xFFFF, o);
    o = b.writeUInt16LE(f.select.usWidth & 0xFFFF, o);
    o = b.writeInt16LE(f.select.sFamilyClass | 0, o);
    o = b.writeInt16LE(f.select.fontFormat | 0, o);
    o = b.writeInt16LE(f.select.shAvgCharWidth | 0, o);
    o = b.writeInt16LE(f.select.shAscent | 0, o);
    o = b.writeInt16LE(f.select.shDescent | 0, o);
    o = b.writeInt16LE(f.select.shLineGap | 0, o);
    o = b.writeInt16LE(f.select.shXHeight | 0, o);
    o = b.writeInt16LE(f.select.shCapHeight | 0, o);
    o = b.writeUInt16LE(f.select.usType & 0xFFFF, o);
    if (o !== recordLen) throw new Error(`selection record length mismatch: ${o} != ${recordLen}`);
    chunks.push(b);
  }
  return Buffer.concat(chunks);
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

  // One entry per FACE for the selection bin (family name + style flags +
  // OS/2 scoring data + file basename the loader resolves against fontsPath).
  const selectionFaces = [];

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
        selectionFaces.push({ select: font.select, path: basename(fp), faceIndex: font.faceIndex });
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
      selectionFaces.push({ select: info.select, path: basename(fp), faceIndex: 0 });
    }
  }

  console.log(`  Parsed ${families.size} font families.`);

  // 4. Copy font files to vendor/fonts/ and build the file list.
  //
  // IMPORTANT: the sdkjs web font loader (common/Drawings/Externals.js,
  // CFontFileLoader.LoadFontArrayBuffer) unconditionally XOR-"decodes" the
  // first 32 bytes of every fetched font with the odttf GUID — ONLYOFFICE
  // servers always serve fonts in that obfuscated form. Serving plain TTFs
  // therefore CORRUPTS their header on load (FT_Open_Face fails silently and
  // every non-embedded font becomes unusable, which surfaced as typed text
  // falling back to the current embedded subset font's empty .notdef glyph).
  // XOR is symmetric, so store the files pre-obfuscated the same way.
  const ODTTF_GUID = [0xA0, 0x66, 0xD6, 0x20, 0x14, 0x96, 0x47, 0xFA, 0x95, 0x69, 0xB8, 0x50, 0xB0, 0x41, 0x49, 0x48];
  await mkdir(FONTS_DIR, { recursive: true });
  const finalFileNames = []; // index-aligned with filesList

  for (const fp of filesList) {
    const name = basename(fp);
    const dest = join(FONTS_DIR, name);
    const data = await readFile(fp);
    for (let i = 0; i < Math.min(32, data.length); i++) data[i] ^= ODTTF_GUID[i % 16];
    await writeFile(dest, data);
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

  // __fonts_ranges powers the character→font fallback (FontPickerByCharacter
  // + the WASM engine's _InitializeFontsRanges). Without it, any character
  // missing from a PDF's embedded subset font — i.e. every newly TYPED
  // character — renders as a .notdef box.
  const ranges = buildFontRanges(infos.map((i) => i[0]));

  // The font-selection table. Without it the engine's name resolution has no
  // candidates (only the built-in ASCW3 dummy) and every font name resolves to
  // ASCW3 → typed text renders as .notdef boxes. See buildSelectionBin().
  const selectionBin = buildSelectionBin(selectionFaces);

  const jsContent = `// Generated by generate-allfonts.mjs — do not edit.
// Font data from ONLYOFFICE/core-fonts (Apache-2.0 / OFL / GPL).
(function(w) {
w["__fonts_files"] = ${JSON.stringify(finalFileNames)};
w["__fonts_infos"] = ${JSON.stringify(infos)};
w["__fonts_ranges"] = ${JSON.stringify(ranges)};
// Record format version of g_fonts_selection_bin (v2: utf8 names + recordLen
// framing + usType). Read by CFontSelect.fromStream in common/libfont/map.js.
w["__all_fonts_js_version__"] = 2;
// font_selection.bin equivalent: per-face selection records (panose, unicode/
// codepage ranges, weight/width/metrics) that drive the engine's font-name
// resolution and per-character fallback. Consumed by BOTH the JS side
// (CFontSelectList.Init) and the WASM engine (_InitializeFontsBase64).
w["g_fonts_selection_bin"] = "${selectionBin.toString("base64")}";
})(window);
`;

  await mkdir(dirname(ALLFONTS_PATH), { recursive: true });
  await writeFile(ALLFONTS_PATH, jsContent, "utf8");

  console.log(`\n  Generated AllFonts.js with ${infos.length} families, ${finalFileNames.length} files, ${selectionFaces.length} selection records (${selectionBin.length} bytes).`);
  console.log(`  → ${ALLFONTS_PATH}`);
  console.log(`  → ${FONTS_DIR}/`);
  console.log("\nDone!");
}

main().catch((err) => {
  console.error("Error:", err);
  process.exit(1);
});
