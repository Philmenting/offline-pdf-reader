#!/usr/bin/env node
/**
 * Vendors the offline OCR stack (tesseract.js) under vendor/ocr/:
 *
 *   vendor/ocr/tesseract.min.js       – main-thread API
 *   vendor/ocr/worker.min.js          – web worker
 *   vendor/ocr/core/…                 – WASM cores (SIMD + non-SIMD LSTM)
 *   vendor/ocr/lang/{deu,eng}.traineddata.gz – language models
 *
 * Sources are the pinned npm packages below (installed into node_modules on
 * demand), so the runtime never touches the network — matching the app's
 * offline guarantee. Output is gitignored and produced on demand, like the
 * ONLYOFFICE engine build.
 *
 * Usage: node scripts/fetch-ocr.mjs
 */
import { mkdir, cp, stat } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = join(ROOT, "vendor", "ocr");

const PACKAGES = ["tesseract.js@6.0.1", "@tesseract.js-data/deu@1.0.0", "@tesseract.js-data/eng@1.0.0"];

async function exists(p) {
  try { await stat(p); return true; } catch { return false; }
}

function run(cmd, args) {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, args, { stdio: "inherit", cwd: ROOT, shell: process.platform === "win32" });
    p.on("error", reject);
    p.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`${cmd} exited ${code}`))));
  });
}

async function main() {
  if (await exists(join(OUT, "tesseract.min.js"))) {
    console.log("✓ vendor/ocr already present — skipping (delete it to re-fetch)");
    return;
  }

  const need = [];
  for (const pkg of PACKAGES) {
    const name = pkg.startsWith("@") ? pkg.slice(0, pkg.indexOf("@", 1)) : pkg.split("@")[0];
    if (!(await exists(join(ROOT, "node_modules", ...name.split("/"))))) need.push(pkg);
  }
  if (need.length) {
    console.log(`↓ installing ${need.join(", ")} …`);
    await run("npm", ["install", "--no-save", "--no-audit", "--no-fund", ...need]);
  }

  const nm = join(ROOT, "node_modules");
  await mkdir(join(OUT, "core"), { recursive: true });
  await mkdir(join(OUT, "lang"), { recursive: true });

  await cp(join(nm, "tesseract.js", "dist", "tesseract.min.js"), join(OUT, "tesseract.min.js"));
  await cp(join(nm, "tesseract.js", "dist", "worker.min.js"), join(OUT, "worker.min.js"));

  // LSTM cores only (what v6 uses); SIMD for modern CPUs + plain fallback.
  for (const f of [
    "tesseract-core-simd-lstm.js", "tesseract-core-simd-lstm.wasm", "tesseract-core-simd-lstm.wasm.js",
    "tesseract-core-lstm.js", "tesseract-core-lstm.wasm", "tesseract-core-lstm.wasm.js",
  ]) {
    await cp(join(nm, "tesseract.js-core", f), join(OUT, "core", f));
  }

  await cp(join(nm, "@tesseract.js-data", "deu", "4.0.0", "deu.traineddata.gz"), join(OUT, "lang", "deu.traineddata.gz"));
  await cp(join(nm, "@tesseract.js-data", "eng", "4.0.0", "eng.traineddata.gz"), join(OUT, "lang", "eng.traineddata.gz"));

  console.log(`✓ OCR stack vendored under ${OUT}`);
}

main().catch((e) => { console.error(e); process.exit(1); });
