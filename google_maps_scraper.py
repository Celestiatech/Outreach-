"""
Google Maps Outreach Lead Generator
-------------------------------------
Searches Google Maps for a keyword, visits each business listing, extracts
contact information, analyzes the site for common issues, scores each lead,
and saves everything to an enriched CSV file ready for outreach campaigns.

Extracted fields
~~~~~~~~~~~~~~~~
keyword, name, address, phone, website, rating, reviews, category,
email, linkedin, twitter, facebook, instagram, contact_page,
issues, lead_score

Usage:
    python google_maps_scraper.py --keyword "digital agency London" --results 50 --output leads.csv
"""

import argparse
import csv
import logging
import os
import re
import time
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Seconds to wait between page navigations (be polite to servers).
REQUEST_DELAY = 1.5

# Maximum time (ms) to wait for a network response in Playwright.
NAVIGATION_TIMEOUT = 20_000

# Maximum time (ms) to wait for Google Maps panel selectors.
PANEL_TIMEOUT = 15_000

# Maximum redirects to follow per requests.Session request.
MAX_REDIRECTS = 5

# Rotate a realistic User-Agent to reduce the chance of being blocked.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Regex that matches common email formats while rejecting obvious invalid forms.
EMAIL_REGEX = re.compile(
    r"(?<![.\w])"
    r"[a-zA-Z0-9]"
    r"(?:[a-zA-Z0-9._%+\-]*[a-zA-Z0-9])?"
    r"@"
    r"[a-zA-Z0-9]"
    r"(?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)*"
    r"\.[a-zA-Z]{2,}"
    r"(?![\w.])"
)

# Phone number pattern – matches most international and domestic formats.
PHONE_REGEX = re.compile(
    r"(?<!\d)"
    r"(\+?\d[\d\s\-().]{6,18}\d)"
    r"(?!\d)"
)

# Lead scoring weights
SCORE_HAS_EMAIL = 3
SCORE_HAS_PHONE = 2
SCORE_HAS_WEBSITE = 1
SCORE_HAS_LINKEDIN = 1
SCORE_HAS_SOCIAL = 1
SCORE_PER_ISSUE = -1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP helpers (website enrichment)
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

    Only ``http`` and ``https`` schemes are permitted.  URLs that resolve to
    loopback, link-local, or private network addresses are rejected to guard
    against SSRF.
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

    Issues checked
    ~~~~~~~~~~~~~~
    * No SSL – URL scheme is ``http`` rather than ``https``
    * Missing ``<title>`` tag
    * Missing ``<meta name="description">`` tag
    * No contact page link found anywhere on the page
    """
    issues: list[str] = []

    if urlparse(url).scheme == "http":
        issues.append("No SSL (site not secure)")

    soup = BeautifulSoup(html, "html.parser")

    if not soup.find("title") or not (soup.title.string or "").strip():
        issues.append("Missing page title")

    meta_desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if not meta_desc or not (meta_desc.get("content") or "").strip():
        issues.append("Missing meta description")

    contact_link = soup.find("a", href=True, string=re.compile(r"contact", re.I))
    if not contact_link:
        contact_link = soup.find("a", href=re.compile(r"contact", re.I))
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
    """Look for a "Contact" link on the page and return its absolute URL."""
    for tag in soup.find_all("a", href=True):
        href = tag.get("href", "")
        text = tag.get_text(separator=" ")
        if re.search(r"contact", href, re.I) or re.search(r"contact", text, re.I):
            absolute = urljoin(url, href)
            if _is_safe_url(absolute):
                return absolute
    return ""


def _extract_social_links(soup: BeautifulSoup) -> dict[str, str]:
    """Scan all anchor hrefs for well-known social media domains."""
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


def _extract_phone_from_soup(soup: BeautifulSoup) -> str:
    """Return the first phone number found on the page, or an empty string."""
    tel_tag = soup.find("a", href=re.compile(r"^tel:", re.I))
    if tel_tag:
        return tel_tag["href"].replace("tel:", "").strip()
    text = soup.get_text(separator=" ")
    match = PHONE_REGEX.search(text)
    return match.group(1).strip() if match else ""


def enrich_from_website(url: str, session: requests.Session) -> dict:
    """
    Fetch *url*, parse its HTML, and return enrichment data.

    Returns a dict with keys: ``emails``, ``phone``, ``linkedin``,
    ``twitter``, ``facebook``, ``instagram``, ``contact_page``, ``issues``.
    """
    result: dict = {
        "emails": set(),
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
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()

    result["emails"] = _extract_emails_from_soup(soup)
    result["phone"] = _extract_phone_from_soup(soup)
    result.update(_extract_social_links(soup))

    contact_url = _find_contact_page_url(url, soup)
    result["contact_page"] = contact_url
    result["issues"] = analyze_website(url, html)

    if contact_url and contact_url != url:
        logger.info("Visiting contact page: %s", contact_url)
        try:
            contact_resp = session.get(contact_url, timeout=15, allow_redirects=True)
            contact_resp.raise_for_status()
            contact_soup = BeautifulSoup(contact_resp.text, "html.parser")
            for el in contact_soup(["script", "style", "noscript"]):
                el.decompose()
            result["emails"].update(_extract_emails_from_soup(contact_soup))
            if not result["phone"]:
                result["phone"] = _extract_phone_from_soup(contact_soup)
        except requests.RequestException as exc:
            logger.warning("Could not fetch contact page %s – %s", contact_url, exc)

    return result


# ---------------------------------------------------------------------------
# Lead scoring
# ---------------------------------------------------------------------------


def score_lead(data: dict) -> int:
    """
    Assign a numeric lead quality score based on available contact data.

    * ``+3`` if at least one email address was found
    * ``+2`` if a phone number was found
    * ``+1`` if a website is listed on Google Maps
    * ``+1`` if a LinkedIn profile was found
    * ``+1`` if any other social media link was found
    * ``-1`` for each issue detected by :func:`analyze_website`
    """
    score = 0
    if data.get("emails"):
        score += SCORE_HAS_EMAIL
    if data.get("phone"):
        score += SCORE_HAS_PHONE
    if data.get("website"):
        score += SCORE_HAS_WEBSITE
    if data.get("linkedin"):
        score += SCORE_HAS_LINKEDIN
    if any(data.get(p) for p in ("twitter", "facebook", "instagram")):
        score += SCORE_HAS_SOCIAL
    score += len(data.get("issues", [])) * SCORE_PER_ISSUE
    return score


# ---------------------------------------------------------------------------
# Google Maps scraping via Playwright
# ---------------------------------------------------------------------------


def _safe_text(page, selector: str, timeout: int = 5_000) -> str:
    """Return inner text of the first matching element, or '' on timeout/error."""
    try:
        el = page.locator(selector).first
        el.wait_for(timeout=timeout)
        return (el.inner_text() or "").strip()
    except Exception:
        return ""


def _safe_attr(page, selector: str, attr: str, timeout: int = 5_000) -> str:
    """Return an attribute of the first matching element, or '' on timeout/error."""
    try:
        el = page.locator(selector).first
        el.wait_for(timeout=timeout)
        return (el.get_attribute(attr) or "").strip()
    except Exception:
        return ""


def scrape_google_maps(keyword: str, num_results: int = 50) -> list[dict]:
    """
    Search Google Maps for *keyword* and scrape up to *num_results* business
    listings.

    Returns a list of dicts with keys:
    ``keyword``, ``name``, ``address``, ``phone``, ``website``, ``rating``,
    ``reviews``, ``category``, ``email``, ``linkedin``, ``twitter``,
    ``facebook``, ``instagram``, ``contact_page``, ``issues``, ``lead_score``.
    """
    records: list[dict] = []
    http_session = _create_session()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT)

        search_url = (
            f"https://www.google.com/maps/search/{requests.utils.quote(keyword)}"
        )
        logger.info("Opening Google Maps: %s", search_url)

        try:
            page.goto(search_url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            logger.warning("Timed out loading Google Maps search page.")
            browser.close()
            return records

        # Dismiss consent / cookie dialog if present (EU regions).
        for btn_text in ("Accept all", "Reject all", "I agree", "Agree"):
            try:
                btn = page.get_by_role("button", name=btn_text)
                if btn.is_visible(timeout=3_000):
                    btn.click()
                    page.wait_for_timeout(1_000)
                    break
            except Exception:
                pass

        # Wait for the results panel to load.
        results_panel_selector = 'div[role="feed"], div.section-result'
        try:
            page.wait_for_selector(results_panel_selector, timeout=PANEL_TIMEOUT)
        except PlaywrightTimeoutError:
            logger.warning("Results panel did not appear; no results collected.")
            browser.close()
            return records

        # Collect listing links by scrolling through the results feed.
        listing_urls: list[str] = []
        logger.info("Collecting listing URLs (target: %d)…", num_results)

        scroll_attempts = 0
        max_scroll_attempts = 30

        while len(listing_urls) < num_results and scroll_attempts < max_scroll_attempts:
            # Gather all currently-visible place links.
            links = page.locator('a[href*="/maps/place/"]').all()
            for link in links:
                href = link.get_attribute("href") or ""
                if href and href not in listing_urls:
                    listing_urls.append(href)
            if len(listing_urls) >= num_results:
                break

            # Scroll the results feed to load more listings.
            try:
                feed = page.locator('div[role="feed"]').first
                feed.evaluate("el => el.scrollBy(0, 600)")
            except Exception:
                page.evaluate("window.scrollBy(0, 600)")

            page.wait_for_timeout(1_200)
            scroll_attempts += 1

        listing_urls = listing_urls[:num_results]
        logger.info("Found %d listing URL(s). Scraping details…", len(listing_urls))

        for idx, listing_url in enumerate(listing_urls, start=1):
            logger.info("[%d/%d] %s", idx, len(listing_urls), listing_url)

            try:
                page.goto(listing_url, wait_until="domcontentloaded")
                page.wait_for_timeout(2_000)
            except PlaywrightTimeoutError:
                logger.warning("Timed out loading listing page, skipping.")
                continue

            # --- Name ---
            name = ""
            for sel in [
                'h1[class*="fontHeadlineLarge"]',
                'h1.DUwDvf',
                'h1[class*="section-hero-header-title"]',
                "h1",
            ]:
                name = _safe_text(page, sel, timeout=4_000)
                if name:
                    break

            # --- Category ---
            category = ""
            for sel in [
                'button[jsaction*="category"]',
                'button.DkEaL',
                '[data-item-id="category"] span',
            ]:
                category = _safe_text(page, sel, timeout=3_000)
                if category:
                    break

            # --- Address ---
            address = ""
            for sel in [
                'button[data-item-id="address"]',
                '[data-tooltip="Copy address"] span',
                'div[data-item-id*="address"] span',
            ]:
                address = _safe_text(page, sel, timeout=3_000)
                if address:
                    break

            # --- Phone ---
            phone = ""
            for sel in [
                'button[data-item-id*="phone"]',
                'a[href^="tel:"]',
                '[data-tooltip="Copy phone number"] span',
            ]:
                phone = _safe_text(page, sel, timeout=3_000)
                if not phone:
                    # Try extracting from href attribute directly.
                    phone = _safe_attr(page, 'a[href^="tel:"]', "href", timeout=3_000)
                    if phone.startswith("tel:"):
                        phone = phone[4:]
                if phone:
                    break

            # --- Website ---
            website = ""
            for sel in [
                'a[data-item-id="authority"]',
                'a[href*="//"][aria-label*="website" i]',
            ]:
                website = _safe_attr(page, sel, "href", timeout=3_000)
                if website:
                    break

            # --- Rating ---
            rating = ""
            for sel in [
                'span[aria-hidden="true"].ceNzKf',
                'div.F7nice span[aria-hidden="true"]',
                'span.MW4etd',
            ]:
                rating = _safe_text(page, sel, timeout=3_000)
                if rating:
                    break

            # --- Reviews count ---
            reviews = ""
            for sel in [
                'span[aria-label*="review" i]',
                'button[jsaction*="review"] span',
                'span.UY7F9',
            ]:
                reviews = _safe_text(page, sel, timeout=3_000)
                # Strip surrounding parentheses if present, e.g. "(123)"
                reviews = reviews.strip("() ")
                if reviews:
                    break

            logger.info(
                "  name=%r  phone=%r  website=%r  rating=%r",
                name, phone, website, rating,
            )

            # --- Enrich from website ---
            enrichment: dict = {
                "emails": set(),
                "phone_web": "",
                "linkedin": "",
                "twitter": "",
                "facebook": "",
                "instagram": "",
                "contact_page": "",
                "issues": [],
            }
            if website and _is_safe_url(website):
                time.sleep(REQUEST_DELAY)
                raw = enrich_from_website(website, http_session)
                enrichment["emails"] = raw["emails"]
                enrichment["phone_web"] = raw["phone"]
                enrichment["linkedin"] = raw["linkedin"]
                enrichment["twitter"] = raw["twitter"]
                enrichment["facebook"] = raw["facebook"]
                enrichment["instagram"] = raw["instagram"]
                enrichment["contact_page"] = raw["contact_page"]
                enrichment["issues"] = raw["issues"]

            # Prefer phone from Google Maps listing; fall back to website phone.
            final_phone = phone or enrichment["phone_web"]

            lead_data = {
                "website": website,
                "phone": final_phone,
                "emails": enrichment["emails"],
                "linkedin": enrichment["linkedin"],
                "twitter": enrichment["twitter"],
                "facebook": enrichment["facebook"],
                "instagram": enrichment["instagram"],
                "issues": enrichment["issues"],
            }
            lead_score = score_lead(lead_data)

            base = {
                "keyword": keyword,
                "name": name,
                "address": address,
                "phone": final_phone,
                "website": website,
                "rating": rating,
                "reviews": reviews,
                "category": category,
                "linkedin": enrichment["linkedin"],
                "twitter": enrichment["twitter"],
                "facebook": enrichment["facebook"],
                "instagram": enrichment["instagram"],
                "contact_page": enrichment["contact_page"],
                "issues": "; ".join(enrichment["issues"]),
                "lead_score": lead_score,
            }

            if enrichment["emails"]:
                for email in sorted(enrichment["emails"]):
                    records.append({**base, "email": email})
            else:
                records.append({**base, "email": ""})

            time.sleep(REQUEST_DELAY)

        browser.close()

    emails_found = sum(1 for r in records if r["email"])
    logger.info(
        "Scraped %d listing(s), found %d email(s). "
        "Lead scores range %s–%s.",
        len(listing_urls),
        emails_found,
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
        Rows produced by :func:`scrape_google_maps`.
    output_path:
        Destination file path (will be created or overwritten).

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
        "keyword", "name", "address", "phone", "website",
        "rating", "reviews", "category", "email",
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
            "Google Maps Outreach Lead Generator – search Google Maps for a "
            "keyword, visit each listing, extract contact info and social links, "
            "analyze each site, score each lead, and save to CSV."
        )
    )
    parser.add_argument(
        "--keyword",
        required=True,
        help="Search keyword or phrase (e.g. 'digital agency London').",
    )
    parser.add_argument(
        "--results",
        type=int,
        default=50,
        metavar="N",
        help="Number of Google Maps listings to inspect (default: 50).",
    )
    parser.add_argument(
        "--output",
        default="leads.csv",
        metavar="FILE",
        help="Path to the output CSV file (default: leads.csv).",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    records = scrape_google_maps(keyword=args.keyword, num_results=args.results)
    if not records:
        logger.warning("No records collected – nothing to save.")
        return
    save_to_csv(records, args.output)


if __name__ == "__main__":
    main()
