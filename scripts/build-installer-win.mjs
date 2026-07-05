#!/usr/bin/env node
/**
 * Builds the Windows setup executable from the bundled desktop app.
 *
 * Wraps dist/Offline-PDF-Editor (produced by `npm run dist:win`) into an NSIS
 * installer with solid LZMA compression — substantially smaller than the ZIP
 * — including Start-menu/desktop shortcuts and an uninstaller registered in
 * Apps & Features. Per-user install, no admin rights required.
 *
 * NSIS cross-builds Windows installers on any platform: `makensis` comes from
 * the `nsis` package (apt install nsis).
 *
 * Usage: node scripts/build-installer-win.mjs
 */
import { stat, readFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");
const APP_DIR = join(ROOT, "dist", "Offline-PDF-Editor");
const OUT = join(ROOT, "dist", "Offline-PDF-Editor-Setup.exe");
const NSI = join(ROOT, "installer", "windows-installer.nsi");

async function exists(p) { try { await stat(p); return true; } catch { return false; } }

function run(cmd, args) {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, args, { stdio: "inherit" });
    p.on("error", reject);
    p.on("close", (code) => code === 0 ? resolve() : reject(new Error(`${cmd} exited ${code}`)));
  });
}

async function main() {
  console.log("=== Building Windows installer (NSIS) ===\n");

  if (!(await exists(join(APP_DIR, "Offline-PDF-Editor.exe")))) {
    console.error("dist/Offline-PDF-Editor fehlt — erst: npm run dist:win");
    process.exit(1);
  }

  const version = JSON.parse(await readFile(join(ROOT, "package.json"), "utf8")).version;

  // NSIS on POSIX accepts forward slashes in defines; the .nsi itself uses
  // "\*" for the recursive File glob, which makensis normalises.
  await run("makensis", [
    "-V2",
    `-DSRCDIR=${APP_DIR}`,
    `-DOUTFILE=${OUT}`,
    `-DVERSION=${version}`,
    NSI,
  ]);

  const size = (await stat(OUT)).size;
  console.log(`\n  ✓ dist/Offline-PDF-Editor-Setup.exe (${(size / 1024 / 1024).toFixed(0)} MB)`);
  console.log("\nDone!");
}

main().catch((err) => { console.error("Error:", err); process.exit(1); });
