#!/usr/bin/env python3
"""
mirror.py — Static mirror generator for southeasttravel.com.ph
==============================================================
Recursively crawls https://southeasttravel.com.ph, downloads all pages
and assets (CSS, JS, images, fonts), rewrites internal URLs to relative
paths, and writes everything into a self-contained static directory tree.

Usage
-----
    # Mirror into the repository root (run from repo root):
    python3 scripts/mirror.py

    # Mirror to a custom output directory:
    python3 scripts/mirror.py --output /path/to/output

    # Limit crawl depth:
    python3 scripts/mirror.py --max-depth 5

    # Resume an interrupted mirror (skip already-downloaded files):
    python3 scripts/mirror.py --resume

Requirements
------------
    pip install requests beautifulsoup4 tqdm

Serve locally after mirroring
------------------------------
    python3 -m http.server 8080
    # then open http://localhost:8080
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import mimetypes
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional
from urllib.robotparser import RobotFileParser

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit(
        "Missing dependencies. Run:  pip install requests beautifulsoup4 tqdm"
    )

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TARGET_DOMAIN = "southeasttravel.com.ph"
START_URL = "https://southeasttravel.com.ph/"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent  # repo root
REQUEST_DELAY = 0.5          # seconds between requests (be polite)
REQUEST_TIMEOUT = 30         # seconds
MAX_RETRIES = 3
USER_AGENT = (
    "Mozilla/5.0 (compatible; SiteArchiveBot/1.0; "
    "+https://github.com/FillinTheBlanks/FillinTheBlanks-southeasttravel-static)"
)

# File extensions that are treated as assets (not crawled for links but saved)
ASSET_EXTENSIONS = {
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".ico", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".mp4", ".webm",
    ".pdf", ".zip", ".json", ".xml", ".txt", ".map",
}

# HTML-producing content-types
HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_url(url: str, base: str = START_URL) -> Optional[str]:
    """Return the absolute URL, or None if it should be skipped."""
    url = url.strip()
    if not url or url.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    joined = urllib.parse.urljoin(base, url)
    parsed = urllib.parse.urlparse(joined)
    # Only follow same-domain URLs
    host = parsed.netloc.removeprefix("www.")
    if host not in (TARGET_DOMAIN, f"www.{TARGET_DOMAIN}"):
        return None
    # Drop fragment
    clean = parsed._replace(fragment="").geturl()
    return clean


def url_to_local_path(url: str, output_dir: Path) -> Path:
    """
    Convert a URL to a local file path under output_dir.

    Rules
    -----
    - https://example.com/           → <output>/index.html
    - https://example.com/about      → <output>/about/index.html
    - https://example.com/about/     → <output>/about/index.html
    - https://example.com/style.css  → <output>/style.css
    - https://example.com/img/a.png  → <output>/img/a.png
    """
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lstrip("/") or ""

    # Determine if this looks like an asset (has a known file extension)
    suffix = Path(path).suffix.lower()
    if suffix in ASSET_EXTENSIONS:
        local = output_dir / path
    else:
        # Treat as HTML page
        if path == "" or path.endswith("/"):
            local = output_dir / path / "index.html"
        else:
            local = output_dir / path / "index.html"

    return local.resolve()


def local_path_to_url_path(local: Path, output_dir: Path) -> str:
    """Return the URL-path component for a saved local file."""
    rel = local.relative_to(output_dir.resolve())
    return "/" + str(rel).replace("\\", "/")


def relative_url(from_file: Path, to_file: Path) -> str:
    """
    Compute a relative URL from *from_file* to *to_file*.
    Both paths are absolute local paths.
    """
    from_dir = from_file.parent
    try:
        rel = os.path.relpath(str(to_file), str(from_dir))
    except ValueError:
        # Different drives on Windows – fall back to absolute
        rel = "/" + str(to_file).replace("\\", "/")
    return rel.replace("\\", "/")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    adapter = requests.adapters.HTTPAdapter(max_retries=MAX_RETRIES)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch(session: requests.Session, url: str) -> Optional[requests.Response]:
    """Fetch *url* with retries. Returns the response or None on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if resp.status_code == 200:
                return resp
            elif resp.status_code in (301, 302, 303, 307, 308):
                # Redirected — requests follows automatically
                return resp
            else:
                log.warning("HTTP %d: %s", resp.status_code, url)
                return None
        except requests.exceptions.SSLError:
            # Try HTTP fallback once
            if url.startswith("https://"):
                url = url.replace("https://", "http://", 1)
                continue
            log.error("SSL error: %s", url)
            return None
        except requests.exceptions.ConnectionError as exc:
            log.warning("Connection error (attempt %d/%d): %s — %s",
                        attempt, MAX_RETRIES, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
        except requests.exceptions.Timeout:
            log.warning("Timeout (attempt %d/%d): %s", attempt, MAX_RETRIES, url)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
        except Exception as exc:
            log.error("Unexpected error fetching %s: %s", url, exc)
            return None
    return None


# ---------------------------------------------------------------------------
# URL rewriting
# ---------------------------------------------------------------------------

def rewrite_html(html: str, page_url: str, output_dir: Path,
                 url_map: dict[str, Path]) -> str:
    """
    Parse *html* and rewrite all internal links/src/href attributes to
    relative paths pointing to locally-saved files.

    *url_map* maps canonical URL → local Path (populated as files are saved).
    """
    soup = BeautifulSoup(html, "html.parser")

    attrs_to_rewrite = [
        ("a", "href"),
        ("link", "href"),
        ("script", "src"),
        ("img", "src"),
        ("img", "data-src"),
        ("source", "src"),
        ("source", "srcset"),
        ("video", "src"),
        ("audio", "src"),
        ("iframe", "src"),
        ("form", "action"),
    ]

    current_local = url_map.get(page_url)
    if current_local is None:
        current_local = url_to_local_path(page_url, output_dir)

    for tag_name, attr in attrs_to_rewrite:
        for tag in soup.find_all(tag_name, **{attr: True}):
            original = tag[attr]
            # Handle srcset (comma-separated list of urls)
            if attr == "srcset":
                new_parts = []
                for part in original.split(","):
                    part = part.strip()
                    tokens = part.split()
                    if not tokens:
                        continue
                    raw_url = tokens[0]
                    norm = normalize_url(raw_url, page_url)
                    if norm and norm in url_map:
                        rel = relative_url(current_local, url_map[norm])
                        tokens[0] = rel
                    new_parts.append(" ".join(tokens))
                tag[attr] = ", ".join(new_parts)
                continue

            norm = normalize_url(original, page_url)
            if norm is None:
                continue
            if norm in url_map:
                rel = relative_url(current_local, url_map[norm])
                tag[attr] = rel

    # Also rewrite inline style background-image urls
    for tag in soup.find_all(style=True):
        tag["style"] = rewrite_css_text(tag["style"], page_url, output_dir, url_map, current_local)

    return str(soup)


def rewrite_css_text(css: str, base_url: str, output_dir: Path,
                     url_map: dict[str, Path],
                     from_file: Optional[Path] = None) -> str:
    """Rewrite url(...) references in CSS text."""
    def replacer(m: re.Match) -> str:
        raw = m.group(1).strip().strip("'\"")
        norm = normalize_url(raw, base_url)
        if norm and norm in url_map and from_file:
            return f"url('{relative_url(from_file, url_map[norm])}')"
        return m.group(0)

    return re.sub(r'url\(([^)]+)\)', replacer, css)


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class Crawler:
    def __init__(
        self,
        output_dir: Path,
        max_depth: int = 10,
        resume: bool = False,
        respect_robots: bool = True,
    ):
        self.output_dir = output_dir
        self.max_depth = max_depth
        self.resume = resume
        self.session = make_session()
        self.visited: set[str] = set()     # already fetched URLs
        self.queue: list[tuple[str, int]] = []  # (url, depth)
        self.url_map: dict[str, Path] = {}  # canonical URL → local file
        self.asset_queue: list[str] = []    # assets to download after HTML pass
        self.robot_parser: Optional[RobotFileParser] = None
        if respect_robots:
            self._load_robots()

    # ------------------------------------------------------------------
    def _load_robots(self) -> None:
        robots_url = f"https://{TARGET_DOMAIN}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(robots_url)
        try:
            rp.read()
            self.robot_parser = rp
            log.info("Loaded robots.txt from %s", robots_url)
        except Exception as exc:
            log.warning("Could not load robots.txt: %s", exc)

    def _is_allowed(self, url: str) -> bool:
        if self.robot_parser is None:
            return True
        return self.robot_parser.can_fetch(USER_AGENT, url)

    # ------------------------------------------------------------------
    def run(self) -> None:
        self.queue.append((START_URL, 0))
        total_saved = 0

        while self.queue:
            url, depth = self.queue.pop(0)
            if url in self.visited:
                continue
            if depth > self.max_depth:
                log.debug("Max depth reached, skipping: %s", url)
                continue
            if not self._is_allowed(url):
                log.info("robots.txt disallows: %s", url)
                continue

            self.visited.add(url)
            local_path = url_to_local_path(url, self.output_dir)

            # Resume: skip if file already exists
            if self.resume and local_path.exists():
                log.debug("SKIP (resume): %s", url)
                self.url_map[url] = local_path
                continue

            time.sleep(REQUEST_DELAY)
            resp = fetch(self.session, url)
            if resp is None:
                continue

            # Follow redirect chain — update effective URL
            effective_url = resp.url
            content_type = resp.headers.get("content-type", "").split(";")[0].strip()

            if content_type in HTML_CONTENT_TYPES:
                links, assets = self._process_html(resp, effective_url, local_path)
                self.url_map[url] = local_path
                if effective_url != url:
                    self.url_map[effective_url] = local_path
                # Enqueue discovered links
                for link in links:
                    if link not in self.visited:
                        self.queue.append((link, depth + 1))
                # Enqueue discovered assets
                for asset in assets:
                    if asset not in self.visited:
                        self.asset_queue.append(asset)
                total_saved += 1
                log.info("[%d] HTML saved: %s", total_saved, url)
            else:
                # Save binary asset
                if self._save_asset(resp, local_path):
                    self.url_map[url] = local_path
                    if effective_url != url:
                        self.url_map[effective_url] = local_path
                    total_saved += 1

        # Now download all queued assets
        log.info("Downloading %d asset(s) ...", len(self.asset_queue))
        asset_iter = self.asset_queue
        if HAS_TQDM:
            asset_iter = tqdm(self.asset_queue, desc="Assets", unit="file")
        for asset_url in asset_iter:
            if asset_url in self.visited:
                continue
            self.visited.add(asset_url)
            local_path = url_to_local_path(asset_url, self.output_dir)
            if self.resume and local_path.exists():
                self.url_map[asset_url] = local_path
                continue
            time.sleep(REQUEST_DELAY / 4)
            resp = fetch(self.session, asset_url)
            if resp is None:
                continue
            if self._save_asset(resp, local_path):
                self.url_map[asset_url] = local_path
                if resp.url != asset_url:
                    self.url_map[resp.url] = local_path
                total_saved += 1

        log.info("Total files saved: %d", total_saved)

        # Second pass: rewrite HTML files
        log.info("Rewriting URLs in HTML files ...")
        self._rewrite_all_html()
        log.info("Done.")

    # ------------------------------------------------------------------
    def _process_html(
        self,
        resp: requests.Response,
        page_url: str,
        local_path: Path,
    ) -> tuple[list[str], list[str]]:
        """
        Parse HTML, save raw copy, extract internal links and assets.
        Returns (page_links, asset_urls).
        """
        try:
            html = resp.text
        except Exception as exc:
            log.error("Could not decode HTML for %s: %s", page_url, exc)
            return [], []

        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(html, encoding="utf-8", errors="replace")

        soup = BeautifulSoup(html, "html.parser")
        page_links: list[str] = []
        asset_urls: list[str] = []

        # --- Collect all URLs in the document ---
        for tag, attr in [
            ("a", "href"),
            ("link", "href"),
            ("area", "href"),
        ]:
            for el in soup.find_all(tag, **{attr: True}):
                norm = normalize_url(el[attr], page_url)
                if norm:
                    suffix = Path(urllib.parse.urlparse(norm).path).suffix.lower()
                    if suffix in ASSET_EXTENSIONS:
                        asset_urls.append(norm)
                    else:
                        page_links.append(norm)

        for tag, attr in [
            ("img", "src"),
            ("img", "data-src"),
            ("script", "src"),
            ("source", "src"),
            ("video", "src"),
            ("audio", "src"),
            ("iframe", "src"),
        ]:
            for el in soup.find_all(tag, **{attr: True}):
                norm = normalize_url(el[attr], page_url)
                if norm:
                    asset_urls.append(norm)

        # srcset
        for el in soup.find_all(srcset=True):
            for part in el["srcset"].split(","):
                raw = part.strip().split()[0]
                norm = normalize_url(raw, page_url)
                if norm:
                    asset_urls.append(norm)

        # Inline style background-image
        for el in soup.find_all(style=True):
            for m in re.finditer(r'url\(["\']?([^)"\']+)["\']?\)', el["style"]):
                norm = normalize_url(m.group(1), page_url)
                if norm:
                    asset_urls.append(norm)

        # <style> blocks
        for style_tag in soup.find_all("style"):
            if style_tag.string:
                for m in re.finditer(r'url\(["\']?([^)"\']+)["\']?\)', style_tag.string):
                    norm = normalize_url(m.group(1), page_url)
                    if norm:
                        asset_urls.append(norm)

        return list(dict.fromkeys(page_links)), list(dict.fromkeys(asset_urls))

    # ------------------------------------------------------------------
    def _save_asset(self, resp: requests.Response, local_path: Path) -> bool:
        """Write response content to local_path. Returns True on success."""
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(resp.content)
            # For CSS, queue any url() references
            ct = resp.headers.get("content-type", "").split(";")[0].strip()
            if ct == "text/css" or local_path.suffix.lower() == ".css":
                self._extract_css_urls(resp.text, resp.url)
            return True
        except Exception as exc:
            log.error("Could not save %s: %s", local_path, exc)
            return False

    def _extract_css_urls(self, css: str, base_url: str) -> None:
        for m in re.finditer(r'url\(["\']?([^)"\']+)["\']?\)', css):
            raw = m.group(1).strip()
            norm = normalize_url(raw, base_url)
            if norm and norm not in self.visited:
                self.asset_queue.append(norm)
        # @import
        for m in re.finditer(r'@import\s+["\']([^"\']+)["\']', css):
            norm = normalize_url(m.group(1), base_url)
            if norm and norm not in self.visited:
                self.asset_queue.append(norm)

    # ------------------------------------------------------------------
    def _rewrite_all_html(self) -> None:
        """
        Walk all saved .html files, rewrite internal URLs to relative paths.
        """
        html_files = list(self.output_dir.rglob("*.html"))
        if HAS_TQDM:
            html_files = tqdm(html_files, desc="Rewriting HTML", unit="file")

        # Build reverse map: local_path → canonical URL for context
        path_to_url: dict[Path, str] = {v: k for k, v in self.url_map.items()}

        for html_file in html_files:
            try:
                html = html_file.read_text(encoding="utf-8", errors="replace")
                page_url = path_to_url.get(html_file.resolve(), START_URL)
                rewritten = rewrite_html(html, page_url, self.output_dir, self.url_map)
                html_file.write_text(rewritten, encoding="utf-8", errors="replace")
            except Exception as exc:
                log.error("Could not rewrite %s: %s", html_file, exc)

        # Also rewrite CSS url() references
        css_files = list(self.output_dir.rglob("*.css"))
        for css_file in css_files:
            try:
                css = css_file.read_text(encoding="utf-8", errors="replace")
                page_url = path_to_url.get(css_file.resolve(), START_URL)
                rewritten = rewrite_css_text(css, page_url, self.output_dir,
                                             self.url_map, css_file)
                css_file.write_text(rewritten, encoding="utf-8", errors="replace")
            except Exception as exc:
                log.error("Could not rewrite CSS %s: %s", css_file, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global REQUEST_DELAY
    parser = argparse.ArgumentParser(
        description="Mirror southeasttravel.com.ph into a static directory."
    )
    parser.add_argument(
        "--output", "-o",
        default=str(DEFAULT_OUTPUT),
        help=f"Output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--max-depth", "-d",
        type=int,
        default=10,
        help="Maximum crawl depth (default: 10)",
    )
    parser.add_argument(
        "--resume", "-r",
        action="store_true",
        help="Skip files that already exist locally",
    )
    parser.add_argument(
        "--no-robots",
        action="store_true",
        help="Ignore robots.txt",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY,
        help=f"Seconds between requests (default: {REQUEST_DELAY})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    REQUEST_DELAY = args.delay

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    log.info("Output directory: %s", output)
    log.info("Starting mirror of %s", START_URL)

    crawler = Crawler(
        output_dir=output,
        max_depth=args.max_depth,
        resume=args.resume,
        respect_robots=not args.no_robots,
    )
    crawler.run()


if __name__ == "__main__":
    main()
