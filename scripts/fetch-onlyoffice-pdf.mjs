#!/usr/bin/env node
/**
 * Fetches the prebuilt ONLYOFFICE sdkjs PDF engine assets into vendor/onlyoffice/.
 *
 * These files are AGPL-3.0 (Copyright Ascensio System SIA). They are NOT
 * committed to this repo (see .gitignore); we pin them to a specific upstream
 * commit for reproducibility and download them on demand.
 *
 * Usage: node scripts/fetch-onlyoffice-pdf.mjs
 */
import { mkdir, writeFile, stat } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");

// Pinned ONLYOFFICE/sdkjs commit (master @ 2026-05-19).
const SDKJS_REF = "72b0421c0bbf9d01eed9cf14834ae47eb2df1b50";
const RAW = (p) => `https://raw.githubusercontent.com/ONLYOFFICE/sdkjs/${SDKJS_REF}/${p}`;

// Prebuilt engine assets that make up the standalone PDF renderer.
// Paths are relative to the sdkjs repo root -> mirrored under vendor/onlyoffice/.
const ASSETS = [
  "pdf/src/engine/drawingfile.js",
  "pdf/src/engine/drawingfile.wasm",
  "pdf/src/engine/cmap.bin",
  "pdf/src/engine/viewer.js",
];

const DEST_ROOT = join(ROOT, "vendor", "onlyoffice");

async function exists(p) {
  try { await stat(p); return true; } catch { return false; }
}

async function download(relPath) {
  const url = RAW(relPath);
  const dest = join(DEST_ROOT, relPath);
  await mkdir(dirname(dest), { recursive: true });
  process.stdout.write(`  ↓ ${relPath} ... `);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  const buf = Buffer.from(await res.arrayBuffer());
  await writeFile(dest, buf);
  console.log(`${(buf.length / 1024 / 1024).toFixed(2)} MB`);
}

async function main() {
  console.log(`Fetching ONLYOFFICE sdkjs PDF engine @ ${SDKJS_REF.slice(0, 10)}`);
  console.log(`Destination: ${DEST_ROOT}\n`);
  for (const asset of ASSETS) {
    await download(asset);
  }
  // Write a small provenance marker.
  await writeFile(
    join(DEST_ROOT, "PROVENANCE.json"),
    JSON.stringify({ source: "ONLYOFFICE/sdkjs", ref: SDKJS_REF, license: "AGPL-3.0-only", assets: ASSETS }, null, 2)
  );
  console.log("\n✓ Engine assets ready under vendor/onlyoffice/");
}

main().catch((err) => {
  console.error(`\n✗ Fetch failed: ${err.message}`);
  process.exit(1);
});
