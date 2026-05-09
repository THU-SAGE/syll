# Real-execution DOMPurify integration test

This directory hosts a real-execution sanitizer test that runs the same
`marked.parse(...) → DOMPurify.sanitize(...)` pipeline as the browser, but
under Node + JSDOM, so attacker payloads can be exercised in CI.

By default the test in `tests/test_sanitizer_integration.py` **auto-skips**
when Node or JSDOM is unavailable. To enable it:

```bash
cd tests/sanitizer
npm install jsdom@26
```

(JSDOM is not declared as a project dependency; this directory is its only
home, and `package.json` lives here. The vendored `dompurify.min.js` and
`marked.min.js` from `syll/web/static/vendor/` are loaded into the JSDOM
window — no second copy.)

Once JSDOM is installed, `pytest tests/test_sanitizer_integration.py -v`
runs the malicious-markdown battery (`<script>`, `on*=`, `javascript:`,
`x-data`, `<iframe>`, `<svg onload>`, `data:` URI iframe) and asserts none
of the dangerous bits survive sanitization.
