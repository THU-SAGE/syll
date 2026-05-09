# Vendored static dependencies

These files were downloaded from public CDNs and pinned for offline use under a
strict Content-Security-Policy (`script-src 'self'` family). Replacing them
requires updating the SHA-256 below; `tests/test_csp.py` asserts they match.

| File | Source URL | Version | SHA-256 |
|---|---|---|---|
| `d3.min.js` | https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js | d3@7 | `f2094bbf6141b359722c4fe454eb6c4b0f0e42cc10cc7af921fc158fceb86539` |
| `cal-heatmap.min.js` | https://cdn.jsdelivr.net/npm/cal-heatmap@4.2.4/dist/cal-heatmap.min.js | 4.2.4 | `e2beb98eb0d44c27baa3d070b85cba6d1f18484b09b827fc33da226cf39dd14c` |
| `cal-heatmap.css` | https://cdn.jsdelivr.net/npm/cal-heatmap@4.2.4/dist/cal-heatmap.css | 4.2.4 | `2d70c33309b23ec708d1667cbb4bc1c4763b096154ec1d012eddcdca3850f76e` |
| `chart.umd.min.js` | https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js | 4.4.0 | `0e2326c6868072bec1592760c6729043caeea2960a2b46cee6a2192aac6abff0` |
| `marked.min.js` | https://cdn.jsdelivr.net/npm/marked@14.1.3/marked.min.js | 14.1.3 | `1092c3ce33dd737edd02e780263f1476bf18278d81817af6403df6840f8034ab` |
| `highlight.min.js` | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js | 11.9.0 | `837a6fa5b0c736b52bbde2b2b6190f305da3fc9ed41681db5321507057b5c846` |
| `github-dark.min.css` | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css | 11.9.0 | `9f208d022102b1d0c7aebfecd8e42ca7997d5de636649d2b31ea63093d809019` |
| `github.min.css` | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css | 11.9.0 | `3a9a5def8b9c311e5ae43abde85c63133185eed4f0d9f67fea4b00a8308cf066` |
| `alpinejs.min.js` | https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js | 3.14.1 | `358d9afbb1ab5befa2f48061a30776e5bcd7707f410a606ba985f98bc3b1c034` |
| `dompurify.min.js` | https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js | 3.1.6 | `c0845096a7c4a6741f362ac506c94c1c7d27dc603bcc1bf64a587f76f2dbe3a1` |

## Phase 1a notes

- `marked` and `alpinejs` were originally loaded unpinned in `index.html` (lines
  8265 and 8267); we pin to a specific 14.x / 3.x stable here.
- `alpinejs.min.js` is the **standard** Alpine 3 build, which requires CSP
  `'unsafe-eval'`. Phase 1c migrates to `@alpinejs/csp` after auditing every
  `x-*` expression in `index.html`.
- Google Fonts CSS (`fonts.googleapis.com`) and woff2 files (`fonts.gstatic.com`)
  remain external for Phase 1a (allowed in `style-src` / `font-src`). Phase 1d
  vendors them locally and tightens CSP.
