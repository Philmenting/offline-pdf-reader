/**
 * Shared font-registry machinery for the ONLYOFFICE PDF engine, used by both
 * generate-allfonts.mjs (full core-fonts set) and build-desktop-win.mjs
 * (slim Western subset for the desktop package).
 *
 * Responsibilities:
 *   • Parse TTF/OTF/TTC metadata: family/style plus everything the engine's
 *     font-selection scorer needs (panose, unicode/codepage ranges, weight,
 *     width, metrics from the OS/2, head and post tables).
 *   • Serialise g_fonts_selection_bin (font_selection.bin v2 record format,
 *     read by sdkjs common/libfont/map.js CFontSelect.fromStream AND by the
 *     WASM engine via _InitializeFontsBase64 / core ApplicationFonts.cpp
 *     NSFonts::FromBuffer). Without this table the engine's entire font-name
 *     resolution has a single candidate — the built-in ASCW3 checkbox
 *     mini-font — and all text shaped with it renders as .notdef boxes.
 *   • Render AllFonts.js.
 *   • odttf (de)obfuscation: the sdkjs web font loader XOR-"decodes" the
 *     first 32 bytes of every fetched font with the odttf GUID (ONLYOFFICE
 *     servers serve fonts obfuscated). Font files must therefore be stored
 *     pre-obfuscated, and must be de-obfuscated before parsing.
 *
 * License: AGPL-3.0-only.
 */
import { buildFontRanges } from "./font-ranges.mjs";

export const ODTTF_GUID = [0xA0, 0x66, 0xD6, 0x20, 0x14, 0x96, 0x47, 0xFA, 0x95, 0x69, 0xB8, 0x50, 0xB0, 0x41, 0x49, 0x48];

// XOR the first 32 bytes with the odttf GUID, in place. Symmetric: applying
// it to a plain font obfuscates it for serving; applying it to an obfuscated
// font restores the parseable original.
export function odttfToggle(buf) {
  for (let i = 0; i < Math.min(32, buf.length); i++) buf[i] ^= ODTTF_GUID[i % 16];
  return buf;
}

// ── TTF parsing ─────────────────────────────────────────────────────────

function readU16BE(buf, off) { return (buf[off] << 8) | buf[off + 1]; }
function readI16BE(buf, off) { const v = readU16BE(buf, off); return v > 32767 ? v - 65536 : v; }
function readU32BE(buf, off) { return ((buf[off] << 24) | (buf[off+1] << 16) | (buf[off+2] << 8) | buf[off+3]) >>> 0; }

function parseTTFTables(buf, base = 0) {
  if (buf.length < base + 12) return null;
  const sig = readU32BE(buf, base);
  // TrueType: 0x00010000 or 'true'; OpenType: 'OTTO'
  if (sig !== 0x00010000 && sig !== 0x74727565 && sig !== 0x4F54544F) return null;

  const numTables = readU16BE(buf, base + 4);
  const tables = {};
  for (let i = 0; i < numTables; i++) {
    const off = base + 12 + i * 16;
    const tag = String.fromCharCode(buf[off], buf[off+1], buf[off+2], buf[off+3]);
    tables[tag] = { offset: readU32BE(buf, off + 8), length: readU32BE(buf, off + 12) };
  }
  return tables;
}

function parseNameTable(buf, tables) {
  const t = tables["name"];
  if (!t) return {};
  const off = t.offset;
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

function parseHeadTable(buf, tables) {
  const t = tables["head"];
  if (!t) return null;
  const off = t.offset;
  return {
    unitsPerEm: readU16BE(buf, off + 18),
    macStyle: readU16BE(buf, off + 44),
  };
}

function parsePostTable(buf, tables) {
  const t = tables["post"];
  if (!t || t.length < 16) return null;
  return { isFixedPitch: readU32BE(buf, t.offset + 12) !== 0 };
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
// scores fonts by. Missing tables degrade to zeros — a zeroed field simply
// contributes no penalty discrimination.
function buildSelectInfo(buf, tables, family, style) {
  const os2 = parseOS2Table(buf, tables);
  const head = parseHeadTable(buf, tables);
  const post = parsePostTable(buf, tables);

  const bold = style === "bold" || style === "bolditalic";
  const italic = style === "italic" || style === "bolditalic";

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

// Parse a single-face TTF/OTF. Returns null if not a font.
export function parseFontFile(buf) {
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

// Parse a TrueType Collection. Returns an array of faces or null.
export function parseTTC(buf) {
  const tag = String.fromCharCode(buf[0], buf[1], buf[2], buf[3]);
  if (tag !== "ttcf") return null;
  const numFonts = readU32BE(buf, 8);
  const fonts = [];
  for (let i = 0; i < numFonts; i++) {
    const offset = readU32BE(buf, 12 + i * 4);
    const tables = parseTTFTables(buf, offset);
    if (!tables) continue;
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

// ── Registry building ───────────────────────────────────────────────────

/**
 * Build the complete font registry from a list of fonts.
 * @param {Array<{name: string, data: Buffer, ext: string}>} fonts
 *        name: served file name (basename); data: PLAIN (de-obfuscated) bytes.
 * @returns {{fileNames: string[], infos: any[][], selectionFaces: any[]}}
 */
export function buildRegistry(fonts) {
  // families: Map<familyName, { regular, italic, bold, bolditalic }>
  const families = new Map();
  const fileNames = [];
  const fileIndexMap = new Map();
  // one entry per FACE for the selection bin
  const selectionFaces = [];

  const getFileIndex = (name) => {
    if (fileIndexMap.has(name)) return fileIndexMap.get(name);
    const idx = fileNames.length;
    fileNames.push(name);
    fileIndexMap.set(name, idx);
    return idx;
  };

  const addFace = (fileName, face, faceIndex) => {
    const fileIdx = getFileIndex(fileName);
    if (!families.has(face.family)) families.set(face.family, {});
    const fam = families.get(face.family);
    if (!fam[face.style]) fam[face.style] = { fileIndex: fileIdx, faceIndex };
    selectionFaces.push({ select: face.select, path: fileName, faceIndex });
  };

  for (const { name, data, ext } of fonts) {
    if (ext === ".ttc") {
      const faces = parseTTC(data);
      if (!faces) continue;
      for (const face of faces) addFace(name, face, face.faceIndex);
    } else {
      const face = parseFontFile(data);
      if (!face) continue;
      addFace(name, face, 0);
    }
  }

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

  return { fileNames, infos, selectionFaces };
}

// ── g_fonts_selection_bin serializer ────────────────────────────────────
//
// "font_selection.bin" v2 record layout, all integers little-endian:
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
export function buildSelectionBin(faces) {
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

// Render the complete AllFonts.js from a built registry.
export function renderAllFontsJs({ fileNames, infos, selectionFaces }) {
  const ranges = buildFontRanges(infos.map((i) => i[0]));
  const selectionBin = buildSelectionBin(selectionFaces);

  return `// Generated by scripts/fonts-lib.mjs — do not edit.
// Font data from ONLYOFFICE/core-fonts (Apache-2.0 / OFL / GPL).
(function(w) {
w["__fonts_files"] = ${JSON.stringify(fileNames)};
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
}
