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
import { mkdir, rm, stat, cp, writeFile } from "node:fs/promises";
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
