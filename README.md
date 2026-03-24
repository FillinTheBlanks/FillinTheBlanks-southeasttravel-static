Southeast Travel — Static Mirror
Static mirror of **[southeasttravel.com.ph](https://southeasttravel.com.ph)**, served via GitHub Pages.
| | |
|---|---|
| **Source site** | https://southeasttravel.com.ph |
| **Last mirrored** | 2026-03-24 |
| **Mirror script** | [`scripts/mirror.py`](scripts/mirror.py) |
---
## Serving the site locally
```bash
# From the repository root:
python3 -m http.server 8080
```
Then open <http://localhost:8080> in your browser.
---
## Re-running the mirror
The mirror is fully reproducible. To refresh the local copy with the latest
content from the live site:
### Prerequisites
```bash
pip install requests beautifulsoup4 tqdm
```
### Run
```bash
# From the repository root — mirrors into the same directory:
python3 scripts/mirror.py
# Options:
#   --output <dir>     Write files to a custom directory (default: repo root)
#   --max-depth <n>    Limit crawl depth (default: 10)
#   --resume           Skip files that already exist (incremental update)
#   --no-robots        Ignore robots.txt (use responsibly)
#   --delay <secs>     Seconds between requests (default: 0.5)
#   --verbose          Debug logging
python3 scripts/mirror.py --resume
```
The script will:
1. Crawl all pages reachable from `https://southeasttravel.com.ph/`.
2. Download CSS, JavaScript, images, fonts, and other assets.
3. Rewrite all internal URLs to relative paths so the site works without
   the original domain.
4. Save everything under the output directory, preserving original URL paths
   (e.g. `/about/` → `about/index.html`).
---
## Repository structure
```
.
├── index.html              ← Homepage (southeasttravel.com.ph/)
├── about/
│   └── index.html
├── destinations/
│   └── index.html
├── ... (other mirrored pages)
├── assets/                 ← CSS, JS, images, fonts
│   ├── css/
│   ├── js/
│   └── images/
├── scripts/
│   └── mirror.py           ← Mirroring script
└── README.md               ← This file
```
---
## GitHub Pages
This repository is configured to be served via **GitHub Pages**.  
Enable it in **Settings → Pages → Deploy from branch → `main` / `(root)`**.
---
## Notes
- The mirror respects `robots.txt` by default.
- A polite request delay of 0.5 s is applied between HTTP requests.
- External resources (CDNs, third-party embeds) that point outside
  `southeasttravel.com.ph` are left as-is.