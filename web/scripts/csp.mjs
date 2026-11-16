// Builds the Content-Security-Policy for the static export and writes it as an nginx snippet.
//
// Next.js puts a few inline <script> tags in every page (the React Server Component payload).
// A strict CSP forbids inline scripts unless each one is allowed by its SHA-256 hash, and the
// hashes change with every build, so they are computed here, after `next build`, from out/.
//
// Usage: node scripts/csp.mjs out > security-headers.conf
import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const root = process.argv[2] ?? "out";

function* htmlFiles(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) yield* htmlFiles(path);
    else if (entry.name.endsWith(".html")) yield path;
  }
}

const hashes = new Set();
for (const file of htmlFiles(root)) {
  const html = readFileSync(file, "utf8");
  // Inline scripts only: a <script src=...> is covered by 'self'.
  for (const [, attrs, body] of html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)) {
    if (/\bsrc=/.test(attrs) || body.length === 0) continue;
    hashes.add(`'sha256-${createHash("sha256").update(body, "utf8").digest("base64")}'`);
  }
}
if (hashes.size === 0) throw new Error(`no inline scripts found under ${root}: wrong directory?`);

const policy = [
  "default-src 'self'",
  `script-src 'self' ${[...hashes].sort().join(" ")}`,
  // React and motion set style="" attributes at runtime; styles cannot run code.
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:", // the paper grain and stamp masks are data: SVGs
  "font-src 'self'", // next/font self-hosts the fonts
  "connect-src 'self'", // the API is on the same origin, under /api
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

// `always`: send the headers on error responses too. Repeated in every location that adds its
// own headers, because nginx drops inherited add_header lines as soon as a location has one.
process.stdout.write(
  [
    `add_header Content-Security-Policy "${policy}" always;`,
    `add_header X-Content-Type-Options "nosniff" always;`,
    `add_header Referrer-Policy "same-origin" always;`,
    `add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;`,
    `add_header Cross-Origin-Opener-Policy "same-origin" always;`,
    "",
  ].join("\n"),
);
console.error(`csp: ${hashes.size} inline script hashes`);
