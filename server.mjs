#!/usr/bin/env node
/**
 * Tiny static dev server for the offline PDF editor.
 *
 * Serves `public/` and `vendor/` with the headers the ONLYOFFICE engine needs:
 *  - correct `application/wasm` MIME for the WASM module
 *  - COOP/COEP so SharedArrayBuffer / wasm threads are available (cross-origin
 *    isolation). Harmless for the single-origin offline case.
 *
 * No external dependencies — Node's built-in http + fs only.
 */
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { join, normalize, extname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL(".", import.meta.url));
const PORT = process.env.PORT ? Number(process.env.PORT) : 3000;

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".wasm": "application/wasm",
  ".bin": "application/octet-stream",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".pdf": "application/pdf",
  ".ttf": "font/ttf",
  ".otf": "font/otf",
  ".ttc": "font/collection",
};

// Map URL prefixes to on-disk directories.
// `/sdkjs/` is an alias for the vendored ONLYOFFICE sdkjs root: the editor core
// hardcodes asset paths like `../../../../sdkjs/common/libfont/engine/fonts.js`
// (font engine, cursors, spell, stamps). Served from our page at "/", those
// resolve to `/sdkjs/…`, so we mount the vendored sdkjs there too.
const MOUNTS = [
  { prefix: "/sdkjs/", dir: join(ROOT, "vendor", "onlyoffice", "sdkjs") },
  { prefix: "/vendor/", dir: join(ROOT, "vendor") },
  { prefix: "/", dir: join(ROOT, "public") },
];

function resolvePath(urlPath) {
  const clean = normalize(decodeURIComponent(urlPath.split("?")[0]));
  if (clean.includes("..")) return null; // path traversal guard
  for (const m of MOUNTS) {
    if (clean.startsWith(m.prefix)) {
      const rel = clean.slice(m.prefix.length);
      return join(m.dir, rel || "index.html");
    }
  }
  return null;
}

const server = createServer(async (req, res) => {
  let filePath = resolvePath(req.url || "/");
  if (!filePath) {
    res.writeHead(400).end("Bad request");
    return;
  }
  try {
    let info = await stat(filePath);
    if (info.isDirectory()) filePath = join(filePath, "index.html");
    const body = await readFile(filePath);
    res.writeHead(200, {
      "Content-Type": MIME[extname(filePath)] || "application/octet-stream",
      "Cross-Origin-Opener-Policy": "same-origin",
      "Cross-Origin-Embedder-Policy": "require-corp",
      "Cache-Control": "no-cache",
    });
    res.end(body);
  } catch {
    res.writeHead(404, { "Content-Type": "text/plain" }).end("Not found");
  }
});

server.listen(PORT, () => {
  console.log(`offline-pdf-reader dev server: http://localhost:${PORT}`);
});
