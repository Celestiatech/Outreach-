"""
Bing Email Scraper
------------------
Searches Bing for a given keyword, visits each result page,
extracts email addresses found on those pages, and saves the
results to a CSV file.

Usage:
    python bing_email_scraper.py --keyword "site:example.com" --results 10 --output emails.csv
"""

import argparse
import csv
import logging
import os
import re
import time
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BING_SEARCH_URL = "https://www.bing.com/search"

# Number of results returned per Bing search page.
RESULTS_PER_PAGE = 10

# Rotate a realistic User-Agent to reduce the chance of being blocked.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Regex that matches common email formats while rejecting obvious
# invalid forms (consecutive dots, leading/trailing dots in local
# part, domain with trailing dot).
EMAIL_REGEX = re.compile(
    r"(?<![.\w])"          # not preceded by dot or word char (no leading dot)
    r"[a-zA-Z0-9]"         # must start with alphanumeric
    r"(?:[a-zA-Z0-9._%+\-]*[a-zA-Z0-9])?"  # middle (no trailing dot/special)
    r"@"
    r"[a-zA-Z0-9]"         # domain starts with alphanumeric
    r"(?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?"      # domain label body
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)*"  # further labels
    r"\.[a-zA-Z]{2,}"      # TLD (no trailing dot)
    r"(?![\w.])"           # not followed by word char or dot
)

# Seconds to wait between HTTP requests (be polite to servers).
REQUEST_DELAY = 1.5

# Maximum redirects to follow per request.
MAX_REDIRECTS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def _create_session() -> requests.Session:
    """Return a :class:`requests.Session` pre-configured with shared headers."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.max_redirects = MAX_REDIRECTS
    return session


def _is_safe_url(url: str) -> bool:
    """
    Return *True* if *url* is safe to fetch.

    Only ``http`` and ``https`` schemes are permitted.  URLs that
    resolve to loopback, link-local, or private network addresses
    are rejected to guard against SSRF.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    hostname = parsed.hostname or ""
    if not hostname:
        return False

    # Reject obvious private / loopback hostnames without a DNS lookup.
    blocked_prefixes = (
        "localhost",
        "127.",
        "10.",
        "192.168.",
        "172.16.",
        "172.17.",
        "172.18.",
        "172.19.",
        "172.20.",
        "172.21.",
        "172.22.",
        "172.23.",
        "172.24.",
        "172.25.",
        "172.26.",
        "172.27.",
        "172.28.",
        "172.29.",
        "172.30.",
        "172.31.",
        "169.254.",
        "::1",
        "0.0.0.0",
    )
    for prefix in blocked_prefixes:
        if hostname == prefix or hostname.startswith(prefix):
            return False

    return True


def bing_search(keyword: str, num_results: int = 10) -> list[str]:
    """
    Query Bing and return a list of result URLs.

    Parameters
    ----------
    keyword:
        The search query string.
    num_results:
        Maximum number of result URLs to return.

    Returns
    -------
    list[str]
        Deduplicated list of result page URLs.
    """
    urls: list[str] = []
    page = 0

    session = _create_session()

    while len(urls) < num_results:
        params = {
            "q": keyword,
            "first": page * RESULTS_PER_PAGE + 1,  # Bing pagination offset
            "count": RESULTS_PER_PAGE,
        }

        try:
            response = session.get(
                BING_SEARCH_URL, params=params, timeout=15
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Bing search request failed: %s", exc)
            break

        soup = BeautifulSoup(response.text, "html.parser")

        # Bing wraps each organic result in <li class="b_algo">
        results = soup.select("li.b_algo h2 a")
        if not results:
            logger.info("No more Bing results found.")
            break

        for tag in results:
            href = tag.get("href", "")
            if _is_safe_url(href) and href not in urls:
                urls.append(href)
                if len(urls) >= num_results:
                    break

        page += 1
        time.sleep(REQUEST_DELAY)

    logger.info("Collected %d result URL(s) from Bing.", len(urls))
    return urls[:num_results]


def extract_emails_from_url(url: str, session: requests.Session) -> set[str]:
    """
    Visit *url*, parse its HTML, and return all email addresses found.

    Also checks the page's mailto: links for additional addresses.

    Parameters
    ----------
    url:
        The page to visit.
    session:
        A pre-configured :class:`requests.Session` to reuse.

    Returns
    -------
    set[str]
        Lowercase email addresses discovered on the page.
    """
    emails: set[str] = set()

    if not _is_safe_url(url):
        logger.warning("Skipping unsafe URL: %s", url)
        return emails

    try:
        response = session.get(url, timeout=15, allow_redirects=True)
        response.raise_for_status()
    except requests.TooManyRedirects:
        logger.warning("Too many redirects for %s – skipping.", url)
        return emails
    except requests.RequestException as exc:
        logger.warning("Could not fetch %s – %s", url, exc)
        return emails

    soup = BeautifulSoup(response.text, "html.parser")

    # Extract emails from visible text nodes only (skip scripts/styles).
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    visible_text = soup.get_text(separator=" ")
    found = EMAIL_REGEX.findall(visible_text)
    emails.update(e.lower() for e in found)

    # Also pull mailto: hrefs which may not appear in plain text.
    for tag in soup.select("a[href^='mailto:']"):
        raw = unquote(tag["href"]).replace("mailto:", "").split("?")[0].strip()
        if EMAIL_REGEX.match(raw):
            emails.add(raw.lower())

    return emails


def scrape(keyword: str, num_results: int = 10) -> list[dict]:
    """
    High-level entry point: search Bing, visit each page, collect emails.

    Parameters
    ----------
    keyword:
        Search term.
    num_results:
        How many Bing result pages to inspect.

    Returns
    -------
    list[dict]
        Records with keys ``keyword``, ``url``, and ``email``.
        One row per (url, email) pair; if no email is found the
        ``email`` field is an empty string.
    """
    urls = bing_search(keyword, num_results)
    records: list[dict] = []

    session = _create_session()

    for url in urls:
        logger.info("Visiting: %s", url)
        emails = extract_emails_from_url(url, session)
        time.sleep(REQUEST_DELAY)

        if emails:
            for email in sorted(emails):
                records.append(
                    {"keyword": keyword, "url": url, "email": email}
                )
        else:
            records.append({"keyword": keyword, "url": url, "email": ""})

    logger.info(
        "Found %d email(s) across %d page(s).",
        sum(1 for r in records if r["email"]),
        len(urls),
    )
    return records


def save_to_csv(records: list[dict], output_path: str) -> None:
    """
    Write *records* to a CSV file at *output_path*.

    Parameters
    ----------
    records:
        Rows produced by :func:`scrape`.
    output_path:
        Destination file path (will be created or overwritten).
        The path is resolved to an absolute path; any parent directories
        that do not exist are created automatically.

    Raises
    ------
    ValueError
        If *output_path* resolves to a directory rather than a file.
    """
    resolved = os.path.realpath(output_path)
    if os.path.isdir(resolved):
        raise ValueError(
            f"Output path '{output_path}' is a directory, not a file."
        )

    parent = os.path.dirname(resolved)
    if parent:
        os.makedirs(parent, exist_ok=True)

    fieldnames = ["keyword", "url", "email"]

    with open(resolved, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)

    logger.info("Results saved to '%s' (%d row(s)).", resolved, len(records))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search Bing for a keyword, visit result pages, and extract email addresses."
    )
    parser.add_argument(
        "--keyword",
        required=True,
        help="Search keyword or phrase (e.g. 'contact us site:example.com').",
    )
    parser.add_argument(
        "--results",
        type=int,
        default=10,
        metavar="N",
        help="Number of Bing result pages to inspect (default: 10).",
    )
    parser.add_argument(
        "--output",
        default="emails.csv",
        metavar="FILE",
        help="Path to the output CSV file (default: emails.csv).",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    records = scrape(keyword=args.keyword, num_results=args.results)
    save_to_csv(records, args.output)


if __name__ == "__main__":
    main()
