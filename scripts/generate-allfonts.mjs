#!/usr/bin/env node
/**
 * Generates the font registry (AllFonts.js) and fonts directory required by
 * the ONLYOFFICE PDF engine.
 *
 * Downloads TTF files from the ONLYOFFICE/core-fonts GitHub repository,
 * parses font metadata (name/OS/2/head/post tables), groups fonts into
 * families with style variants, and produces:
 *
 *   vendor/onlyoffice/sdkjs/common/AllFonts.js
 *   vendor/fonts/<name>.ttf          (odttf-obfuscated, see below)
 *
 * The generated AllFonts.js populates:
 *   __fonts_files / __fonts_infos   font registry (AscFonts.checkAllFonts)
 *   __fonts_ranges                  char→font fallback (FontPickerByCharacter
 *                                   + the WASM engine's _InitializeFontsRanges)
 *   g_fonts_selection_bin           the font-selection table driving ALL
 *                                   font-name resolution; without it every
 *                                   name resolves to the ASCW3 dummy font and
 *                                   typed text renders as .notdef boxes.
 *
 * Font files are stored odttf-OBFUSCATED (first 32 bytes XORed): the sdkjs
 * web font loader unconditionally XOR-"decodes" every fetched font, because
 * ONLYOFFICE servers always serve fonts in that form. Serving plain TTFs
 * would corrupt them on load. See scripts/fonts-lib.mjs.
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
import { buildRegistry, renderAllFontsJs, odttfToggle } from "./fonts-lib.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");
const FONTS_DIR = join(ROOT, "vendor", "fonts");
const ALLFONTS_PATH = join(ROOT, "vendor", "onlyoffice", "sdkjs", "common", "AllFonts.js");

const CORE_FONTS_REF = "master";
const CORE_FONTS_TARBALL = `https://codeload.github.com/ONLYOFFICE/core-fonts/tar.gz/${CORE_FONTS_REF}`;
const CACHE_DIR = join(ROOT, ".build");
const TAR_PATH = join(CACHE_DIR, "core-fonts.tar.gz");
const EXTRACT_DIR = join(CACHE_DIR, "core-fonts");

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

  // 2. Collect and read all font files
  const fontPaths = await collectFontFiles(EXTRACT_DIR);
  console.log(`  Found ${fontPaths.length} font files.`);

  const fonts = [];
  for (const fp of fontPaths) {
    fonts.push({ name: basename(fp), data: await readFile(fp), ext: extname(fp).toLowerCase() });
  }

  // 3. Build the registry (families, style variants, selection records)
  const registry = buildRegistry(fonts);
  console.log(`  Parsed ${registry.infos.length} font families, ${registry.selectionFaces.length} faces.`);

  // 4. Copy the referenced font files to vendor/fonts/, odttf-obfuscated
  //    (the web font loader XOR-decodes every fetched font — see fonts-lib).
  await mkdir(FONTS_DIR, { recursive: true });
  const byName = new Map(fonts.map((f) => [f.name, f]));
  for (const name of registry.fileNames) {
    // note: data buffers are only used once — safe to toggle in place
    await writeFile(join(FONTS_DIR, name), odttfToggle(byName.get(name).data));
  }
  console.log(`  Copied ${registry.fileNames.length} font files to vendor/fonts/ (odttf-obfuscated).`);

  // 5. Generate AllFonts.js
  const jsContent = renderAllFontsJs(registry);
  await mkdir(dirname(ALLFONTS_PATH), { recursive: true });
  await writeFile(ALLFONTS_PATH, jsContent, "utf8");

  console.log(`\n  Generated AllFonts.js with ${registry.infos.length} families, ${registry.fileNames.length} files, ${registry.selectionFaces.length} selection records.`);
  console.log(`  → ${ALLFONTS_PATH}`);
  console.log(`  → ${FONTS_DIR}/`);
  console.log("\nDone!");
}

main().catch((err) => {
  console.error("Error:", err);
  process.exit(1);
});
