#!/usr/bin/env python3
"""
Mirror script for southeasttravel.com.ph

Crawls the live site and saves a static copy with rewritten internal URLs
so the mirror works from any static-file host (e.g. GitHub Pages).
"""

import argparse
import logging
import os
import re
import time
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

SEED_URL = "https://southeasttravel.com.ph/"
DOMAIN = "southeasttravel.com.ph"
USER_AGENT = "SoutheastTravelMirrorBot/1.0 (+https://github.com)"

# File extensions considered binary (downloaded as-is, not parsed for links)
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".webm", ".ogg", ".mp3",
    ".pdf", ".zip", ".gz",
}

# Extensions considered text/HTML that we should parse for links
PAGE_EXTS = {"", ".html", ".htm", ".php", ".asp", ".aspx", "/"}

# CSS / JS are text but parsed differently
ASSET_TEXT_EXTS = {".css", ".js", ".json", ".xml", ".txt", ".map"}

logger = logging.getLogger("mirror")


def _is_internal(url: str) -> bool:
    """Return True if *url* points to the same domain we are mirroring."""
    parsed = urlparse(url)
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname or ""
    return host == "" or host == DOMAIN or host.endswith("." + DOMAIN)


def _normalise_url(url: str) -> str:
    """Strip fragment, ensure trailing slash on directory-like paths."""
    parsed = urlparse(url)
    path = parsed.path
    # Remove fragment
    url = urlunparse(parsed._replace(fragment=""))
    return url


def _url_to_filepath(url: str, output_dir: str) -> str:
    """Convert an absolute URL to a local file-system path."""
    parsed = urlparse(url)
    path = parsed.path.lstrip("/")

    if not path or path.endswith("/"):
        path = os.path.join(path, "index.html")

    # If path has no extension, treat it as a directory with index.html
    _, ext = os.path.splitext(path)
    if not ext:
        path = os.path.join(path, "index.html")

    return os.path.join(output_dir, path)


def _relative_path(from_file: str, to_file: str) -> str:
    """Compute a relative path from *from_file* to *to_file*."""
    from_dir = os.path.dirname(from_file)
    rel = os.path.relpath(to_file, from_dir)
    # Use POSIX separators for URLs
    return rel.replace(os.sep, "/")


def _check_robots(seed_url: str) -> RobotFileParser | None:
    """Fetch and parse robots.txt.  Returns None when unreachable so the
    caller can treat the failure as "allow everything"."""
    parsed = urlparse(seed_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
        return rp
    except Exception:
        logger.warning("Could not fetch robots.txt – allowing all URLs")
        return None


def _fetch(url: str, session: requests.Session, delay: float) -> requests.Response | None:
    """Fetch *url* with polite delay. Returns None on failure."""
    time.sleep(delay)
    try:
        resp = session.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def _extract_links_html(html: str, base_url: str) -> set[str]:
    """Extract internal links from an HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    urls: set[str] = set()

    # <a href>, <link href>, <script src>, <img src>, <source src/srcset>
    tag_attrs = [
        ("a", "href"),
        ("link", "href"),
        ("script", "src"),
        ("img", "src"),
        ("img", "data-src"),
        ("source", "src"),
        ("source", "srcset"),
        ("video", "src"),
        ("video", "poster"),
    ]

    for tag_name, attr in tag_attrs:
        for tag in soup.find_all(tag_name):
            val = tag.get(attr)
            if not val:
                continue
            # srcset can have multiple URLs
            if attr == "srcset":
                for part in val.split(","):
                    part = part.strip().split()[0] if part.strip() else ""
                    if part:
                        full = urljoin(base_url, part)
                        if _is_internal(full):
                            urls.add(full)
            else:
                full = urljoin(base_url, val)
                if _is_internal(full):
                    urls.add(full)

    # Inline CSS url() references
    for style_tag in soup.find_all("style"):
        if style_tag.string:
            urls.update(_extract_links_css(style_tag.string, base_url))

    # Inline style attributes
    for tag in soup.find_all(style=True):
        urls.update(_extract_links_css(tag["style"], base_url))

    return urls


def _extract_links_css(css_text: str, base_url: str) -> set[str]:
    """Extract url() references from CSS text."""
    urls: set[str] = set()
    for match in re.finditer(r'url\(["\']?([^"\')\s]+)["\']?\)', css_text):
        ref = match.group(1)
        if ref.startswith("data:"):
            continue
        full = urljoin(base_url, ref)
        if _is_internal(full):
            urls.add(full)
    return urls


def _rewrite_html(html: str, page_url: str, output_dir: str) -> str:
    """Rewrite internal absolute URLs in HTML to relative paths."""
    soup = BeautifulSoup(html, "html.parser")
    page_file = _url_to_filepath(page_url, output_dir)

    tag_attrs = [
        ("a", "href"),
        ("link", "href"),
        ("script", "src"),
        ("img", "src"),
        ("img", "data-src"),
        ("source", "src"),
        ("video", "src"),
        ("video", "poster"),
    ]

    for tag_name, attr in tag_attrs:
        for tag in soup.find_all(tag_name):
            val = tag.get(attr)
            if not val:
                continue
            full = urljoin(page_url, val)
            if _is_internal(full):
                target_file = _url_to_filepath(full, output_dir)
                tag[attr] = _relative_path(page_file, target_file)

    # Rewrite srcset
    for tag in soup.find_all(["img", "source"], srcset=True):
        parts = []
        for part in tag["srcset"].split(","):
            tokens = part.strip().split()
            if tokens:
                full = urljoin(page_url, tokens[0])
                if _is_internal(full):
                    target_file = _url_to_filepath(full, output_dir)
                    tokens[0] = _relative_path(page_file, target_file)
                parts.append(" ".join(tokens))
        tag["srcset"] = ", ".join(parts)

    # Rewrite inline style url() references
    for tag in soup.find_all(style=True):
        tag["style"] = _rewrite_css_urls(tag["style"], page_url, page_file, output_dir)

    for style_tag in soup.find_all("style"):
        if style_tag.string:
            style_tag.string = _rewrite_css_urls(style_tag.string, page_url, page_file, output_dir)

    return str(soup)


def _rewrite_css_urls(css_text: str, base_url: str, from_file: str, output_dir: str) -> str:
    """Rewrite url() references in CSS text to relative paths."""
    def replacer(match):
        ref = match.group(1)
        if ref.startswith("data:"):
            return match.group(0)
        full = urljoin(base_url, ref)
        if _is_internal(full):
            target_file = _url_to_filepath(full, output_dir)
            return f'url("{_relative_path(from_file, target_file)}")'
        return match.group(0)

    return re.sub(r'url\(["\']?([^"\')\s]+)["\']?\)', replacer, css_text)


def _strip_url_for_dedup(url: str) -> str:
    """Strip query string and fragment for deduplication."""
    return url.split("#")[0].split("?")[0]


def _save(filepath: str, content: bytes) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(content)


def mirror(
    output_dir: str,
    max_depth: int = 10,
    resume: bool = False,
    respect_robots: bool = True,
    delay: float = 0.5,
) -> None:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    robots = _check_robots(SEED_URL) if respect_robots else None

    # BFS crawl
    queue: list[tuple[str, int]] = [(_normalise_url(SEED_URL), 0)]
    visited: set[str] = set()
    discovered_assets: set[str] = set()

    pbar = tqdm(desc="Crawling", unit=" pages")

    def _classify_and_queue(link: str, current_depth: int) -> None:
        """Add *link* to the page queue or asset set based on extension."""
        lc = _strip_url_for_dedup(link)
        if lc in visited:
            return
        _, lext = os.path.splitext(urlparse(link).path)
        if lext in PAGE_EXTS or not lext:
            queue.append((_normalise_url(link), current_depth + 1))
        else:
            discovered_assets.add(link)

    while queue:
        url, depth = queue.pop(0)
        url_clean = _strip_url_for_dedup(url)

        if url_clean in visited:
            continue
        visited.add(url_clean)

        if depth > max_depth:
            continue

        if robots and not robots.can_fetch(USER_AGENT, url):
            logger.info("Blocked by robots.txt: %s", url)
            continue

        filepath = _url_to_filepath(url, output_dir)

        if resume and os.path.exists(filepath):
            logger.debug("Skipping (exists): %s", filepath)
            # Still parse if it's HTML to discover links
            _, ext = os.path.splitext(urlparse(url).path)
            if ext in PAGE_EXTS or not ext:
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        html = f.read()
                    for link in _extract_links_html(html, url):
                        _classify_and_queue(link, depth)
                except Exception:
                    pass
            pbar.update(1)
            continue

        resp = _fetch(url, session, delay)
        if resp is None:
            continue

        content_type = resp.headers.get("content-type", "")
        _, ext = os.path.splitext(urlparse(url).path)

        if "text/html" in content_type or (ext in PAGE_EXTS and not ext == ".css"):
            # HTML page — parse for links, rewrite, save
            html = resp.text
            for link in _extract_links_html(html, resp.url):
                _classify_and_queue(link, depth)

            rewritten = _rewrite_html(html, resp.url, output_dir)
            _save(filepath, rewritten.encode("utf-8"))
            logger.info("Saved page: %s", filepath)
        elif "text/css" in content_type or ext == ".css":
            # CSS — parse for url() refs, rewrite, save
            css = resp.text
            for link in _extract_links_css(css, resp.url):
                lc = _strip_url_for_dedup(link)
                if lc not in visited:
                    discovered_assets.add(link)
            rewritten = _rewrite_css_urls(css, resp.url, filepath, output_dir)
            _save(filepath, rewritten.encode("utf-8"))
            logger.info("Saved CSS: %s", filepath)
        else:
            # Binary asset — save as-is
            _save(filepath, resp.content)
            logger.info("Saved asset: %s", filepath)

        pbar.update(1)

    # Download discovered assets that weren't visited yet
    pbar.set_description("Downloading assets")
    asset_queue = list(discovered_assets - visited)
    for url in tqdm(asset_queue, desc="Assets", unit=" files"):
        url_clean = _strip_url_for_dedup(url)
        if url_clean in visited:
            continue
        visited.add(url_clean)

        filepath = _url_to_filepath(url, output_dir)
        if resume and os.path.exists(filepath):
            continue

        if robots and not robots.can_fetch(USER_AGENT, url):
            continue

        resp = _fetch(url, session, delay)
        if resp is None:
            continue

        content_type = resp.headers.get("content-type", "")
        _, ext = os.path.splitext(urlparse(url).path)

        if "text/css" in content_type or ext == ".css":
            css = resp.text
            sub_links = _extract_links_css(css, resp.url)
            rewritten = _rewrite_css_urls(css, resp.url, filepath, output_dir)
            _save(filepath, rewritten.encode("utf-8"))
            # Add newly discovered assets to the queue for later processing
            for link in sub_links:
                lc = _strip_url_for_dedup(link)
                if lc not in visited:
                    discovered_assets.add(link)
        else:
            _save(filepath, resp.content)

    # Process any assets newly discovered from CSS files in the asset pass
    new_assets = list(discovered_assets - visited)
    for url in tqdm(new_assets, desc="CSS sub-assets", unit=" files"):
        url_clean = _strip_url_for_dedup(url)
        if url_clean in visited:
            continue
        visited.add(url_clean)
        filepath = _url_to_filepath(url, output_dir)
        if resume and os.path.exists(filepath):
            continue
        resp = _fetch(url, session, delay)
        if resp:
            _save(filepath, resp.content)

    pbar.close()
    logger.info("Mirror complete. %d URLs processed.", len(visited))


def main():
    parser = argparse.ArgumentParser(description="Mirror southeasttravel.com.ph")
    parser.add_argument(
        "--output",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        help="Output directory (default: repository root)",
    )
    parser.add_argument("--max-depth", type=int, default=10, help="Max crawl depth")
    parser.add_argument("--resume", action="store_true", help="Skip existing files")
    parser.add_argument("--no-robots", action="store_true", help="Ignore robots.txt")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between requests (seconds)")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    mirror(
        output_dir=args.output,
        max_depth=args.max_depth,
        resume=args.resume,
        respect_robots=not args.no_robots,
        delay=args.delay,
    )


if __name__ == "__main__":
    main()
