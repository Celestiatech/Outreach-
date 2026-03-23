"""
Google Outreach Lead Generator (Selenium-powered)
--------------------------------------------------
Searches Google for a keyword using Selenium (JavaScript rendering), visits each result page, 
extracts contact information, analyzes the site for common issues, scores each lead, and
saves everything to an enriched CSV file ready for outreach campaigns.

Extracted fields
~~~~~~~~~~~~~~~~
keyword, url, title, email, phone, linkedin, twitter, facebook, instagram,
contact_page, issues, lead_score

Usage:
    python bing_email_scraper.py --keyword "digital agency UK" --results 20 --output leads.csv

Requirements:
    pip install selenium webdriver-manager
"""

import argparse
import csv
import logging
import os
import re
import time
import json
from urllib.parse import unquote, urljoin, urlparse, quote

import requests
from bs4 import BeautifulSoup

# Import Selenium for JavaScript rendering
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.by import By
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.chrome.service import Service
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Use DuckDuckGo (more scraper-friendly than Google or Bing)
DUCKDUCKGO_SEARCH_URL = "https://html.duckduckgo.com/"
BING_SEARCH_URL = "https://www.bing.com/search"

# Number of results returned per search page.
RESULTS_PER_PAGE = 10

# Default configuration file
CONFIG_FILE = "search_patterns.json"

# Rotate a realistic User-Agent to reduce the chance of being blocked.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.bing.com/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
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

# Phone number pattern – matches most international and domestic formats.
PHONE_REGEX = re.compile(
    r"(?<!\d)"
    r"(\+?\d[\d\s\-().]{6,18}\d)"
    r"(?!\d)"
)

# Seconds to wait between HTTP requests (be polite to servers).
REQUEST_DELAY = 1.5

# Maximum redirects to follow per request.
MAX_REDIRECTS = 5

# Lead scoring weights
SCORE_HAS_EMAIL = 3
SCORE_HAS_PHONE = 2
SCORE_HAS_LINKEDIN = 1
SCORE_HAS_SOCIAL = 1      # Twitter / Facebook / Instagram combined
SCORE_PER_ISSUE = -1      # deducted for every detected issue

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


# ---------------------------------------------------------------------------
# Website analysis
# ---------------------------------------------------------------------------


def analyze_website(url: str, html: str) -> list[str]:
    """
    Inspect *url* and its *html* content for common issues.

    Parameters
    ----------
    url:
        The URL that was fetched (used to check the scheme).
    html:
        Raw HTML source of the page.

    Returns
    -------
    list[str]
        Human-readable issue descriptions.  An empty list means no issues
        were detected.

    Issues checked
    ~~~~~~~~~~~~~~
    * No SSL – URL scheme is ``http`` rather than ``https``
    * Missing ``<title>`` tag
    * Missing ``<meta name="description">`` tag
    * No contact page link found anywhere on the page
    """
    issues: list[str] = []

    # --- SSL check ---
    if urlparse(url).scheme == "http":
        issues.append("No SSL (site not secure)")

    soup = BeautifulSoup(html, "html.parser")

    # --- Title check ---
    if not soup.find("title") or not (soup.title.string or "").strip():
        issues.append("Missing page title")

    # --- Meta description check ---
    meta_desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if not meta_desc or not (meta_desc.get("content") or "").strip():
        issues.append("Missing meta description")

    # --- Contact page check ---
    contact_link = soup.find(
        "a", href=True,
        string=re.compile(r"contact", re.I)
    )
    if not contact_link:
        # Also check hrefs that contain "contact"
        contact_link = soup.find(
            "a",
            href=re.compile(r"contact", re.I)
        )
    if not contact_link:
        issues.append("No contact page link found")

    return issues


# ---------------------------------------------------------------------------
# Site data extraction
# ---------------------------------------------------------------------------


def _extract_emails_from_soup(soup: BeautifulSoup) -> set[str]:
    """Return all emails found in *soup* (visible text + mailto: links)."""
    emails: set[str] = set()

    visible_text = soup.get_text(separator=" ")
    emails.update(e.lower() for e in EMAIL_REGEX.findall(visible_text))

    for tag in soup.select("a[href^='mailto:']"):
        raw = unquote(tag["href"]).replace("mailto:", "").split("?")[0].strip()
        if EMAIL_REGEX.match(raw):
            emails.add(raw.lower())

    return emails


def _find_contact_page_url(url: str, soup: BeautifulSoup) -> str:
    """
    Look for a "Contact" link on the page and return its absolute URL.

    Returns an empty string if none is found.
    """
    candidates = soup.find_all("a", href=True)
    for tag in candidates:
        href = tag.get("href", "")
        text = tag.get_text(separator=" ")
        if re.search(r"contact", href, re.I) or re.search(r"contact", text, re.I):
            absolute = urljoin(url, href)
            if _is_safe_url(absolute):
                return absolute
    return ""


def _extract_social_links(soup: BeautifulSoup) -> dict[str, str]:
    """
    Scan all anchor hrefs for well-known social media domains.

    Returns a dict with keys ``linkedin``, ``twitter``, ``facebook``,
    ``instagram`` (empty string when not found).
    """
    social: dict[str, str] = {
        "linkedin": "",
        "twitter": "",
        "facebook": "",
        "instagram": "",
    }
    patterns = {
        "linkedin": re.compile(r"linkedin\.com", re.I),
        "twitter": re.compile(r"(twitter\.com|x\.com)", re.I),
        "facebook": re.compile(r"facebook\.com", re.I),
        "instagram": re.compile(r"instagram\.com", re.I),
    }
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        for platform, pattern in patterns.items():
            if not social[platform] and pattern.search(href):
                social[platform] = href.strip()
    return social


def _extract_phone(soup: BeautifulSoup) -> str:
    """
    Return the first phone number found on the page, or an empty string.

    Checks ``tel:`` links first (most reliable), then falls back to a
    regex scan of visible text.
    """
    # tel: links are the most reliable signal.
    tel_tag = soup.find("a", href=re.compile(r"^tel:", re.I))
    if tel_tag:
        return tel_tag["href"].replace("tel:", "").strip()

    text = soup.get_text(separator=" ")
    match = PHONE_REGEX.search(text)
    return match.group(1).strip() if match else ""


def extract_site_data(url: str, session: requests.Session) -> dict:
    """
    Fetch *url*, parse its HTML, and return a rich dictionary of lead data.

    The returned dict contains:

    ``emails`` : set[str]
        All email addresses found (homepage + contact page).
    ``title`` : str
        Content of the ``<title>`` tag, or empty string.
    ``phone`` : str
        First phone number found, or empty string.
    ``linkedin`` : str
        LinkedIn profile / page URL, or empty string.
    ``twitter`` : str
        Twitter / X URL, or empty string.
    ``facebook`` : str
        Facebook URL, or empty string.
    ``instagram`` : str
        Instagram URL, or empty string.
    ``contact_page`` : str
        Absolute URL of the contact page, or empty string.
    ``issues`` : list[str]
        Issues detected by :func:`analyze_website`.

    Parameters
    ----------
    url:
        The page to visit.
    session:
        A pre-configured :class:`requests.Session` to reuse.
    """
    result: dict = {
        "emails": set(),
        "title": "",
        "phone": "",
        "linkedin": "",
        "twitter": "",
        "facebook": "",
        "instagram": "",
        "contact_page": "",
        "issues": [],
    }

    if not _is_safe_url(url):
        logger.warning("Skipping unsafe URL: %s", url)
        return result

    try:
        response = session.get(url, timeout=15, allow_redirects=True)
        response.raise_for_status()
    except requests.TooManyRedirects:
        logger.warning("Too many redirects for %s – skipping.", url)
        return result
    except requests.RequestException as exc:
        logger.warning("Could not fetch %s – %s", url, exc)
        return result

    html = response.text

    # Parse once and reuse the soup object throughout.
    soup = BeautifulSoup(html, "html.parser")

    # Strip noise tags before any text extraction.
    for element in soup(["script", "style", "noscript"]):
        element.decompose()

    # --- Title ---
    if soup.title and soup.title.string:
        result["title"] = soup.title.string.strip()

    # --- Emails (homepage) ---
    result["emails"] = _extract_emails_from_soup(soup)

    # --- Phone ---
    result["phone"] = _extract_phone(soup)

    # --- Social media links ---
    result.update(_extract_social_links(soup))

    # --- Contact page ---
    contact_url = _find_contact_page_url(url, soup)
    result["contact_page"] = contact_url

    # --- Website analysis ---
    result["issues"] = analyze_website(url, html)

    # --- Visit contact page to find additional emails ---
    if contact_url and contact_url != url:
        logger.info("Visiting contact page: %s", contact_url)
        try:
            contact_resp = session.get(contact_url, timeout=15, allow_redirects=True)
            contact_resp.raise_for_status()
            contact_soup = BeautifulSoup(contact_resp.text, "html.parser")
            for el in contact_soup(["script", "style", "noscript"]):
                el.decompose()
            result["emails"].update(_extract_emails_from_soup(contact_soup))
            # Prefer phone from contact page if homepage had none.
            if not result["phone"]:
                result["phone"] = _extract_phone(contact_soup)
        except requests.RequestException as exc:
            logger.warning("Could not fetch contact page %s – %s", contact_url, exc)

    return result


# ---------------------------------------------------------------------------
# Lead scoring
# ---------------------------------------------------------------------------


def score_lead(data: dict) -> int:
    """
    Assign a numeric lead quality score based on available contact data.

    Higher is better.  The score is composed of:

    * ``+3`` if at least one email address was found
    * ``+2`` if a phone number was found
    * ``+1`` if a LinkedIn profile was found
    * ``+1`` if any other social media link was found
    * ``-1`` for each issue detected by :func:`analyze_website`

    Parameters
    ----------
    data:
        The dict returned by :func:`extract_site_data`.

    Returns
    -------
    int
        Composite lead score (may be negative for very poor sites).
    """
    score = 0
    if data.get("emails"):
        score += SCORE_HAS_EMAIL
    if data.get("phone"):
        score += SCORE_HAS_PHONE
    if data.get("linkedin"):
        score += SCORE_HAS_LINKEDIN
    if any(data.get(p) for p in ("twitter", "facebook", "instagram")):
        score += SCORE_HAS_SOCIAL
    score += len(data.get("issues", [])) * SCORE_PER_ISSUE
    return score


# ---------------------------------------------------------------------------
# Bing search
# ---------------------------------------------------------------------------


def bing_search(keyword: str, num_results: int = 10) -> list[str]:
    """
    Query Bing and return a deduplicated list of result URLs.

    If Bing yields too few results (or blocks), fall back to DuckDuckGo's
    HTML endpoint which is often more scraper-friendly.
    """
    urls: list[str] = []

    def _collect_from_soup(soup: BeautifulSoup, selectors: list[str]) -> None:
        for selector in selectors:
            for tag in soup.select(selector):
                href = (tag.get("href") or "").strip()
                if not href:
                    continue
                if href.startswith("/"):
                    continue
                if not href.startswith("http"):
                    continue
                if any(host in href for host in ("bing.com", "duckduckgo.com", "google.com")):
                    continue
                if not _is_safe_url(href):
                    continue
                if href in urls:
                    continue
                urls.append(href)
                if len(urls) >= num_results:
                    return
            if len(urls) >= num_results:
                return

    session = _create_session()

    try:
        logger.info("Searching Bing for: %s", keyword)
        params = {"q": keyword, "count": max(num_results, RESULTS_PER_PAGE)}
        resp = session.get(BING_SEARCH_URL, params=params, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        _collect_from_soup(
            soup,
            selectors=[
                "li.b_algo h2 a",
                "main li.b_algo a",
                "a[href]",
            ],
        )

        if len(urls) < num_results:
            logger.info(
                "Bing returned only %d URL(s); trying DuckDuckGo fallback.",
                len(urls),
            )
            ddg_resp = session.post(
                DUCKDUCKGO_SEARCH_URL,
                data={"q": keyword},
                timeout=20,
            )
            ddg_resp.raise_for_status()
            ddg_soup = BeautifulSoup(ddg_resp.text, "html.parser")
            _collect_from_soup(
                ddg_soup,
                selectors=[
                    "a.result__a",
                    "a[data-testid='result-title-a']",
                    "a[href]",
                ],
            )

        logger.info("Collected %d result URL(s).", len(urls))

    except requests.RequestException as exc:
        logger.error("Search request failed: %s", exc)
    except Exception as exc:
        logger.error("Error during search scraping: %s", exc)

    return urls[:num_results]


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def load_search_config(config_path: str = CONFIG_FILE) -> dict:
    """
    Load search patterns from JSON config file.
    
    Parameters
    ----------
    config_path:
        Path to search_patterns.json file
    
    Returns
    -------
    dict
        Configuration with search_patterns and settings
    """
    if not os.path.exists(config_path):
        logger.warning("Config file '%s' not found. Using default patterns.", config_path)
        return {
            "search_patterns": [
                {"name": "Default", "keyword": "web design", "results": 5}
            ],
            "settings": {
                "delay_between_searches": 2,
                "delay_between_sites": 1.5,
            }
        }
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        logger.info("Loaded %d search patterns from %s", len(config.get("search_patterns", [])), config_path)
        return config
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in config file: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Scrape pipeline
# ---------------------------------------------------------------------------


def scrape(keyword: str, num_results: int = 10) -> list[dict]:
    """
    High-level entry point: search Bing, visit each page, collect lead data.

    Parameters
    ----------
    keyword:
        Search term.
    num_results:
        How many Bing result pages to inspect.

    Returns
    -------
    list[dict]
        Flat records ready for CSV output.  Each record contains:
        ``keyword``, ``url``, ``title``, ``email``, ``phone``,
        ``linkedin``, ``twitter``, ``facebook``, ``instagram``,
        ``contact_page``, ``issues``, ``lead_score``.

        One row is emitted per (url, email) pair.  If no email is found
        the ``email`` field is an empty string but the row is still included
        (the other enrichment fields still have value).
    """
    urls = bing_search(keyword, num_results)
    records: list[dict] = []

    session = _create_session()

    for url in urls:
        logger.info("Visiting: %s", url)
        data = extract_site_data(url, session)
        lead_score = score_lead(data)
        time.sleep(REQUEST_DELAY)

        issues_str = "; ".join(data["issues"]) if data["issues"] else ""
        base = {
            "keyword": keyword,
            "url": url,
            "title": data["title"],
            "phone": data["phone"],
            "linkedin": data["linkedin"],
            "twitter": data["twitter"],
            "facebook": data["facebook"],
            "instagram": data["instagram"],
            "contact_page": data["contact_page"],
            "issues": issues_str,
            "lead_score": lead_score,
        }

        if data["emails"]:
            for email in sorted(data["emails"]):
                records.append({**base, "email": email})
        else:
            records.append({**base, "email": ""})

    emails_found = sum(1 for r in records if r["email"])
    logger.info(
        "Found %d email(s) across %d page(s). Lead scores range %s–%s.",
        emails_found,
        len(urls),
        min((r["lead_score"] for r in records), default="n/a"),
        max((r["lead_score"] for r in records), default="n/a"),
    )
    return records


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------


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

    fieldnames = [
        "keyword", "url", "title", "email", "phone",
        "linkedin", "twitter", "facebook", "instagram",
        "contact_page", "issues", "lead_score",
    ]

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
        description=(
            "Google Outreach Lead Generator – search Google for a keyword, "
            "visit result pages, extract contact info and social links, "
            "analyze each site, score each lead, and save to CSV."
        )
    )
    parser.add_argument(
        "--keyword",
        help="Search keyword or phrase (e.g. 'digital agency London'). If not provided, uses search_patterns.json",
    )
    parser.add_argument(
        "--results",
        type=int,
        default=10,
        metavar="N",
        help="Number of result pages to inspect (default: 10).",
    )
    parser.add_argument(
        "--output",
        default="leads.csv",
        metavar="FILE",
        help="Path to the output CSV file (default: leads.csv).",
    )
    parser.add_argument(
        "--config",
        default="search_patterns.json",
        metavar="FILE",
        help="Path to search patterns config file (default: search_patterns.json).",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to existing output file instead of overwriting.",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    all_records: list[dict] = []

    # If keyword is provided, use it directly
    if args.keyword:
        logger.info("Running single search: %s", args.keyword)
        records = scrape(keyword=args.keyword, num_results=args.results)
        all_records.extend(records)
    else:
        # Use config file for multiple search patterns
        config = load_search_config(args.config)
        patterns = config.get("search_patterns", [])
        settings = config.get("settings", {})
        
        if not patterns:
            logger.error("No search patterns found in config file")
            return
        
        delay_between_searches = settings.get("delay_between_searches", 2)
        
        logger.info("Running %d search pattern(s) from config", len(patterns))
        
        for i, pattern in enumerate(patterns):
            keyword = pattern.get("keyword")
            num_results = pattern.get("results", args.results)
            name = pattern.get("name", keyword)
            
            if not keyword:
                logger.warning("Skipping pattern without keyword: %s", pattern)
                continue
            
            logger.info("[%d/%d] Searching: %s (%s)", i + 1, len(patterns), name, keyword)
            records = scrape(keyword=keyword, num_results=num_results)
            all_records.extend(records)
            
            if i < len(patterns) - 1:
                time.sleep(delay_between_searches)
    
    # Save all results
    if all_records:
        save_to_csv(all_records, args.output)
    else:
        logger.warning("No records to save")


if __name__ == "__main__":
    main()

