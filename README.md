# FillinTheBlanks-southeasttravel-static

A reproducible, static mirror of [southeasttravel.com.ph](https://southeasttravel.com.ph), published via **GitHub Pages**.

The mirror is generated with GNU `wget` and stored in the `docs/` directory so GitHub Pages can serve it without any build step.

---

## Repository layout

```
.
├── docs/               # Generated static mirror (GitHub Pages source)
│   ├── .nojekyll       # Tells GitHub Pages to skip Jekyll processing
│   └── 404.html        # Fallback 404 page
├── scripts/
│   └── mirror.sh       # Mirror-generation script (wget-based)
└── .github/
    └── workflows/
        └── mirror.yml  # GitHub Actions workflow
```

---

## Running the mirror locally

**Requirements:** GNU `wget` (version ≥ 1.20 recommended)

```bash
# macOS (via Homebrew)
brew install wget

# Debian / Ubuntu
sudo apt-get install wget
```

Then run:

```bash
bash scripts/mirror.sh
```

The script will:
1. Delete and recreate `docs/`.
2. Crawl `southeasttravel.com.ph` recursively, downloading all pages and page requisites (CSS, JS, images, fonts).
3. Convert internal links so the site works when served from a local directory or GitHub Pages.
4. Write `docs/.nojekyll`.
5. Write `docs/404.html` if the site doesn't already provide one.

---

## GitHub Pages setup

After the first mirror run (which populates `docs/`):

1. Go to your repository on GitHub → **Settings → Pages**.
2. Under **Build and deployment → Source**, select **Deploy from a branch**.
3. Set **Branch** to `main` and **Folder** to `/docs`.
4. Click **Save**.

GitHub will publish the mirror at `https://fillintheblank.github.io/FillinTheBlanks-southeasttravel-static/` (or your configured custom domain).

---

## Triggering the GitHub Action

The workflow (`.github/workflows/mirror.yml`) supports two triggers:

| Trigger | How |
|---|---|
| **Manual** | GitHub → **Actions → Mirror southeasttravel.com.ph → Run workflow** |
| **Scheduled** (optional) | Uncomment the `schedule` block in `mirror.yml` — preconfigured for weekly runs every Sunday at 02:00 UTC |

When the mirror runs and `docs/` has changed, the workflow automatically opens a pull request titled **"Update static mirror (YYYY-MM-DD)"** against `main` for review before merging.

---

## Limitations

- **Dynamic content** (forms, login, server-side personalisation) is not captured.
- **External assets** hosted on third-party CDNs may not be downloaded (wget is restricted to `southeasttravel.com.ph`).
- **JavaScript-rendered pages** — content that requires JavaScript execution to appear in the DOM may be missing or incomplete.
- Pages disallowed by **`robots.txt`** are skipped by wget by default.
- The mirror reflects the site at the time the script was last run; it does not update in real-time.
