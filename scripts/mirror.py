#!/usr/bin/env python3
"""
mirror.py — Static-site mirror of southeasttravel.com.ph

Usage:
    python3 scripts/mirror.py [OPTIONS]

Options:
    --output <dir>     Write files to a custom directory (default: repo root)
    --max-depth <n>    Limit crawl depth (default: 10)
    --resume           Skip files that already exist (incremental update)
    --no-robots        Ignore robots.txt (use responsibly)
    --delay <secs>     Seconds between requests (default: 0.5)
    --verbose          Enable debug logging

Prerequisites:
    pip install requests beautifulsoup4 tqdm
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from collections import deque
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://southeasttravel.com.ph"
BASE_DOMAIN = "southeasttravel.com.ph"
USER_AGENT = "Mozilla/5.0 (compatible; SiteArchiver/1.0)"

# HTML tag → attribute that carries a URL
HTML_URL_ATTRS: list[tuple[str, str]] = [
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
    ("input", "src"),
    ("embed", "src"),
    ("object", "data"),
    ("track", "src"),
    ("use", "href"),
    ("use", "xlink:href"),
]

# Regex for CSS url(...) values
CSS_URL_RE = re.compile(
    r"""url\(\s*(['"]?)(?P<url>[^'"\)\s]+)\1\s*\)""",
    re.IGNORECASE,
)

# Retry settings — total attempts = 1 initial + EXTRA_RETRIES
EXTRA_RETRIES = 1
RETRY_DELAY = 2.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_internal(url: str) -> bool:
    """Return True when *url* belongs to the target domain."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().lstrip("www.")
    return host in ("", BASE_DOMAIN, f"www.{BASE_DOMAIN}")


def _normalise(url: str, base: str = BASE_URL) -> str:
    """Resolve *url* relative to *base* and strip fragment/trailing ?."""
    full = urllib.parse.urljoin(base, url)
    # Drop fragment
    parsed = urllib.parse.urlparse(full)
    clean = parsed._replace(fragment="")
    return urllib.parse.urlunparse(clean)


def _url_to_path(url: str, output_dir: Path) -> Path:
    """
    Map a URL to a filesystem path inside *output_dir*.

    Rules
    -----
    * HTML pages: path ends with '/' or has no extension → ``<path>/index.html``
    * Assets: kept as-is relative to *output_dir*
    """
    parsed = urllib.parse.urlparse(url)
    url_path = parsed.path.lstrip("/")

    if not url_path:
        return output_dir / "index.html"

    suffix = Path(url_path).suffix.lower()
    html_like = suffix in ("", ".html", ".htm", ".php", ".asp", ".aspx")
    path_ends_slash = parsed.path.endswith("/")

    if html_like or path_ends_slash:
        base_path = url_path.rstrip("/")
        if not base_path:
            return output_dir / "index.html"
        return output_dir / base_path / "index.html"

    return output_dir / url_path


def _relative_path(from_file: Path, to_file: Path) -> str:
    """Return a relative URL string from *from_file* to *to_file*."""
    rel = os.path.relpath(to_file, from_file.parent)
    # Always use forward slashes (HTML convention)
    return rel.replace(os.sep, "/")


# ---------------------------------------------------------------------------
# CSS rewriting
# ---------------------------------------------------------------------------


def _rewrite_css_urls(
    css_text: str,
    css_file: Path,
    page_url: str,
    output_dir: Path,
    downloaded: dict[str, Path],
    to_download: list[str],
) -> str:
    """Replace url(...) references inside *css_text* with relative paths."""

    def replace_match(m: re.Match[str]) -> str:
        raw = m.group("url")
        if raw.startswith("data:") or raw.startswith("#"):
            return m.group(0)
        abs_url = _normalise(raw, page_url)
        if not _is_internal(abs_url):
            return m.group(0)
        if abs_url not in downloaded and abs_url not in to_download:
            to_download.append(abs_url)
        target = downloaded.get(abs_url) or _url_to_path(abs_url, output_dir)
        rel = _relative_path(css_file, target)
        quote = m.group(1) or ""
        return f"url({quote}{rel}{quote})"

    return CSS_URL_RE.sub(replace_match, css_text)


# ---------------------------------------------------------------------------
# HTML rewriting
# ---------------------------------------------------------------------------


def _rewrite_html(
    soup: BeautifulSoup,
    page_file: Path,
    page_url: str,
    output_dir: Path,
    downloaded: dict[str, Path],
    to_download: list[str],
    discovered_pages: list[str],
) -> None:
    """
    Mutate *soup* in-place: rewrite internal URLs to relative paths and
    collect new URLs to visit/download.
    """

    def _handle_url(raw: str | None) -> str | None:
        if not raw:
            return None
        raw = raw.strip()
        if not raw or raw.startswith(("data:", "javascript:", "mailto:", "#")):
            return None
        abs_url = _normalise(raw, page_url)
        if not _is_internal(abs_url):
            return None
        return abs_url

    # --- tag attributes ---
    for tag_name, attr in HTML_URL_ATTRS:
        for tag in soup.find_all(tag_name, **{attr: True}):
            raw_val: str = tag[attr]

            # Handle srcset (comma-separated list of "url [descriptor]")
            if attr == "srcset":
                parts = []
                changed = False
                for part in raw_val.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    chunks = part.split()
                    abs_url = _handle_url(chunks[0])
                    if abs_url:
                        target = downloaded.get(abs_url) or _url_to_path(
                            abs_url, output_dir
                        )
                        rel = _relative_path(page_file, target)
                        chunks[0] = rel
                        changed = True
                        if abs_url not in downloaded:
                            to_download.append(abs_url)
                    parts.append(" ".join(chunks))
                if changed:
                    tag[attr] = ", ".join(parts)
                continue

            abs_url = _handle_url(raw_val)
            if not abs_url:
                continue

            target_path = _url_to_path(abs_url, output_dir)
            rel = _relative_path(page_file, target_path)
            tag[attr] = rel

            # Classify as page or asset
            suffix = Path(urllib.parse.urlparse(abs_url).path).suffix.lower()
            is_page = suffix in ("", ".html", ".htm", ".php", ".asp", ".aspx")
            path_ends_slash = urllib.parse.urlparse(abs_url).path.endswith("/")

            if (is_page or path_ends_slash) and tag_name == "a":
                if abs_url not in discovered_pages:
                    discovered_pages.append(abs_url)
            else:
                if abs_url not in downloaded and abs_url not in to_download:
                    to_download.append(abs_url)

    # --- inline style attributes ---
    for tag in soup.find_all(style=True):
        original_style: str = tag["style"]
        new_style = _rewrite_css_urls(
            original_style, page_file, page_url, output_dir, downloaded, to_download
        )
        if new_style != original_style:
            tag["style"] = new_style

    # --- <style> tag bodies ---
    for style_tag in soup.find_all("style"):
        if style_tag.string:
            rewritten = _rewrite_css_urls(
                style_tag.string,
                page_file,
                page_url,
                output_dir,
                downloaded,
                to_download,
            )
            if rewritten != style_tag.string:
                style_tag.string.replace_with(rewritten)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class Fetcher:
    """Thin wrapper around *requests.Session* with retry + robots support."""

    def __init__(
        self,
        delay: float,
        respect_robots: bool,
        verbose: bool,
    ) -> None:
        self._delay = delay
        self._last_request: float = 0.0
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._robot_parser: urllib.robotparser.RobotFileParser | None = None
        self._log = logging.getLogger("mirror")

        if respect_robots:
            self._robot_parser = urllib.robotparser.RobotFileParser()
            robots_url = f"{BASE_URL}/robots.txt"
            self._log.debug("Fetching robots.txt from %s", robots_url)
            try:
                self._robot_parser.set_url(robots_url)
                self._robot_parser.read()
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self._log.warning("Could not read robots.txt: %s", exc)
                self._robot_parser = None

    # ------------------------------------------------------------------
    def can_fetch(self, url: str) -> bool:
        if self._robot_parser is None:
            return True
        return self._robot_parser.can_fetch(USER_AGENT, url)

    # ------------------------------------------------------------------
    def get(self, url: str, stream: bool = False) -> requests.Response | None:
        """Fetch *url*, honouring delay and retrying once on 5xx."""
        if not self.can_fetch(url):
            self._log.info("robots.txt disallows: %s", url)
            return None

        # Polite delay
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)

        for attempt in range(EXTRA_RETRIES + 1):
            try:
                self._log.debug("GET %s (attempt %d)", url, attempt + 1)
                resp = self._session.get(
                    url,
                    timeout=30,
                    stream=stream,
                    allow_redirects=True,
                )
                self._last_request = time.monotonic()

                if resp.status_code == 200:
                    return resp
                if 400 <= resp.status_code < 500:
                    self._log.warning("4xx %s → skipping", url)
                    return None
                if 500 <= resp.status_code < 600 and attempt < EXTRA_RETRIES:
                    self._log.warning(
                        "5xx %s — retrying in %.1fs", url, RETRY_DELAY
                    )
                    time.sleep(RETRY_DELAY)
                    continue
                self._log.warning("HTTP %d for %s", resp.status_code, url)
                return None

            except requests.RequestException as exc:
                self._log.warning("Request error for %s: %s", url, exc)
                if attempt < EXTRA_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
                return None

        return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------


class Crawler:
    def __init__(
        self,
        output_dir: Path,
        max_depth: int,
        resume: bool,
        fetcher: Fetcher,
    ) -> None:
        self._output_dir = output_dir
        self._max_depth = max_depth
        self._resume = resume
        self._fetcher = fetcher
        self._log = logging.getLogger("mirror")

        # url → local Path
        self._downloaded: dict[str, Path] = {}
        self._visited_pages: set[str] = set()
        self._errors: list[str] = []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # BFS queue: (url, depth)
        queue: deque[tuple[str, int]] = deque()
        start_url = _normalise(BASE_URL + "/")
        queue.append((start_url, 0))
        self._visited_pages.add(start_url)

        pages_bar = tqdm(desc="Pages", unit="page", position=0)
        assets_bar = tqdm(desc="Assets", unit="file", position=1)

        try:
            while queue:
                url, depth = queue.popleft()
                self._log.debug("Crawling page: %s (depth=%d)", url, depth)

                page_file = _url_to_path(url, self._output_dir)

                if self._resume and page_file.exists():
                    self._log.debug("Skipping (exists): %s", page_file)
                    pages_bar.update(1)
                    continue

                resp = self._fetcher.get(url)
                if resp is None:
                    self._errors.append(url)
                    continue

                # Follow redirect: use final URL for path mapping
                final_url = resp.url
                if final_url != url:
                    final_url = _normalise(final_url)
                    page_file = _url_to_path(final_url, self._output_dir)

                content_type = resp.headers.get("content-type", "")
                if "text/html" not in content_type:
                    # Treat as asset, not page
                    self._save_binary(final_url, resp.content)
                    assets_bar.update(1)
                    continue

                # Parse HTML
                soup = BeautifulSoup(resp.text, "html.parser")

                discovered_pages: list[str] = []
                to_download: list[str] = []

                _rewrite_html(
                    soup,
                    page_file,
                    final_url,
                    self._output_dir,
                    self._downloaded,
                    to_download,
                    discovered_pages,
                )

                # Save HTML
                page_file.parent.mkdir(parents=True, exist_ok=True)
                page_file.write_text(str(soup), encoding="utf-8")
                self._downloaded[final_url] = page_file
                self._log.debug("Saved page: %s", page_file)
                pages_bar.update(1)

                # Download discovered assets
                for asset_url in to_download:
                    if asset_url in self._downloaded:
                        continue
                    asset_path = self._download_asset(asset_url)
                    if asset_path:
                        self._downloaded[asset_url] = asset_path
                        assets_bar.update(1)

                # Enqueue new pages
                if depth < self._max_depth:
                    for new_url in discovered_pages:
                        if new_url not in self._visited_pages and _is_internal(
                            new_url
                        ):
                            self._visited_pages.add(new_url)
                            queue.append((new_url, depth + 1))

        finally:
            pages_bar.close()
            assets_bar.close()

        self._write_sitemap()
        self._print_summary()

    # ------------------------------------------------------------------
    # Asset download
    # ------------------------------------------------------------------

    def _download_asset(self, url: str) -> Path | None:
        dest = _url_to_path(url, self._output_dir)

        if self._resume and dest.exists():
            self._log.debug("Skipping asset (exists): %s", dest)
            return dest

        resp = self._fetcher.get(url, stream=True)
        if resp is None:
            self._errors.append(url)
            return None

        dest.parent.mkdir(parents=True, exist_ok=True)

        content_type = resp.headers.get("content-type", "")
        if "text/css" in content_type:
            # Rewrite CSS url() references
            css_to_download: list[str] = []
            rewritten = _rewrite_css_urls(
                resp.text,
                dest,
                resp.url,
                self._output_dir,
                self._downloaded,
                css_to_download,
            )
            dest.write_text(rewritten, encoding="utf-8")
            # Recursively download assets referenced from this CSS
            for dep_url in css_to_download:
                if dep_url not in self._downloaded:
                    dep_path = self._download_asset(dep_url)
                    if dep_path:
                        self._downloaded[dep_url] = dep_path
        else:
            self._save_binary(url, resp.content, dest=dest)

        return dest

    def _save_binary(
        self, url: str, content: bytes, dest: Path | None = None
    ) -> None:
        if dest is None:
            dest = _url_to_path(url, self._output_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        self._log.debug("Saved asset: %s", dest)

    # ------------------------------------------------------------------
    # Sitemap + summary
    # ------------------------------------------------------------------

    def _write_sitemap(self) -> None:
        sitemap_path = self._output_dir / "sitemap.txt"
        page_urls = sorted(self._visited_pages)
        sitemap_path.write_text("\n".join(page_urls) + "\n", encoding="utf-8")
        self._log.info("sitemap.txt written with %d URLs", len(page_urls))

    def _print_summary(self) -> None:
        pages = sum(
            1
            for p in self._downloaded.values()
            if p.suffix.lower() in (".html", ".htm", ".php", ".asp", ".aspx")
            or p.name == "index.html"
        )
        assets = len(self._downloaded) - pages
        print(
            f"\n{'=' * 60}\n"
            f"  Mirror complete\n"
            f"  Pages   : {pages}\n"
            f"  Assets  : {assets}\n"
            f"  Errors  : {len(self._errors)}\n"
            f"{'=' * 60}"
        )
        if self._errors:
            print("Errors:")
            for e in self._errors:
                print(f"  {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mirror southeasttravel.com.ph to a static site.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    default_output = str(Path(__file__).parent.parent)
    parser.add_argument(
        "--output",
        default=default_output,
        help=f"Output directory (default: {default_output})",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=10,
        dest="max_depth",
        help="Maximum crawl depth (default: 10)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip files that already exist (incremental update)",
    )
    parser.add_argument(
        "--no-robots",
        action="store_true",
        dest="no_robots",
        help="Ignore robots.txt",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds between requests (default: 0.5)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    output_dir = Path(args.output).resolve()
    print(f"Output directory : {output_dir}")
    print(f"Max depth        : {args.max_depth}")
    print(f"Resume           : {args.resume}")
    print(f"Respect robots   : {not args.no_robots}")
    print(f"Delay            : {args.delay}s")

    fetcher = Fetcher(
        delay=args.delay,
        respect_robots=not args.no_robots,
        verbose=args.verbose,
    )
    crawler = Crawler(
        output_dir=output_dir,
        max_depth=args.max_depth,
        resume=args.resume,
        fetcher=fetcher,
    )
    crawler.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
