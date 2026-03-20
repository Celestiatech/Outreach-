"""
LinkedIn Feed Outreach Lead Generator
--------------------------------------
Logs into LinkedIn, searches the feed for a keyword, scrapes each post's
full description, and extracts contact details (emails, phone numbers) and
HR / hiring signals.  Results are saved to an enriched CSV file ready for
outreach campaigns.

Credentials
~~~~~~~~~~~
Supply your LinkedIn credentials via environment variables *or* CLI flags:

    LINKEDIN_EMAIL    / --email
    LINKEDIN_PASSWORD / --password

Extracted fields
~~~~~~~~~~~~~~~~
keyword, post_url, author_name, author_title, author_company, author_profile,
post_text, email, phone, hr_signal, lead_score

Usage:
    python linkedin_feed_scraper.py --keyword "hiring HR manager" --results 30 --output leads.csv

    # or with explicit credentials
    python linkedin_feed_scraper.py \\
        --email me@example.com --password secret \\
        --keyword "software engineer jobs" --results 50 --output leads.csv
"""

import argparse
import csv
import logging
import os
import re
import time
from urllib.parse import quote_plus, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Milliseconds to wait for page / element transitions.
NAVIGATION_TIMEOUT = 30_000
SELECTOR_TIMEOUT = 10_000

# Seconds between scroll steps (be polite to LinkedIn's servers).
SCROLL_PAUSE = 2.0

# Maximum feed-scroll iterations before giving up.
MAX_SCROLL_ATTEMPTS = 40

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

# Keywords that suggest HR / hiring / recruitment content.
HR_KEYWORDS = re.compile(
    r"\b("
    r"hiring|we.?re hiring|now hiring|join our team|open role|open position"
    r"|job opening|job opportunity|career opportunity|careers"
    r"|recruiter|recruitment|talent acquisition|hr manager|human resources"
    r"|apply now|send your cv|send your resume|send cv|send resume"
    r"|looking for a|looking to hire|seeking a|we are looking"
    r"|job posting|job post|new role|new opportunity"
    r")\b",
    re.IGNORECASE,
)

# Lead scoring weights
SCORE_HAS_EMAIL = 4
SCORE_HAS_PHONE = 2
SCORE_HAS_HR_SIGNAL = 2
SCORE_HAS_COMPANY = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL safety helper
# ---------------------------------------------------------------------------


def _is_safe_url(url: str) -> bool:
    """
    Return *True* if *url* uses ``http`` or ``https`` and does not point at a
    private / loopback address (basic SSRF guard).
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
# Text extraction helpers
# ---------------------------------------------------------------------------


def _extract_emails(text: str) -> list[str]:
    """Return all unique lower-cased email addresses found in *text*."""
    return sorted({m.lower() for m in EMAIL_REGEX.findall(text)})


def _extract_phones(text: str) -> list[str]:
    """Return all unique phone numbers found in *text*."""
    return sorted({m.strip() for m in PHONE_REGEX.findall(text)})


def _has_hr_signal(text: str) -> bool:
    """Return *True* if *text* contains HR / hiring keywords."""
    return bool(HR_KEYWORDS.search(text))


# ---------------------------------------------------------------------------
# Lead scoring
# ---------------------------------------------------------------------------


def score_lead(record: dict) -> int:
    """
    Assign a numeric quality score to a LinkedIn post record.

    * ``+4`` for each email address found
    * ``+2`` for each phone number found
    * ``+2`` if an HR / hiring signal is detected
    * ``+1`` if an author company is known
    """
    score = 0
    emails = [e for e in record.get("email", "").split(";") if e.strip()]
    phones = [p for p in record.get("phone", "").split(";") if p.strip()]

    score += SCORE_HAS_EMAIL * len(emails)
    score += SCORE_HAS_PHONE * len(phones)
    if record.get("hr_signal"):
        score += SCORE_HAS_HR_SIGNAL
    if record.get("author_company"):
        score += SCORE_HAS_COMPANY

    return score


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------


def _safe_text(page, selector: str, timeout: int = SELECTOR_TIMEOUT) -> str:
    """
    Return the inner text of the first element matching *selector*, or
    an empty string if the element is not found within *timeout* ms.
    """
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="attached", timeout=timeout)
        return (loc.inner_text() or "").strip()
    except Exception:
        return ""


def _try_click_see_more(post_locator) -> None:
    """
    Expand a truncated post by clicking its "see more" button, if present.
    Silently ignores failures.
    """
    for label in ("see more", "…see more", "...see more"):
        try:
            btn = post_locator.get_by_text(label, exact=False)
            if btn.count() > 0 and btn.first.is_visible(timeout=2_000):
                btn.first.click(timeout=3_000)
                post_locator.page().wait_for_timeout(800)
                return
        except Exception:
            pass


# ---------------------------------------------------------------------------
# LinkedIn login
# ---------------------------------------------------------------------------


def linkedin_login(page, email: str, password: str) -> bool:
    """
    Navigate to linkedin.com/login and sign in with *email* / *password*.

    Returns *True* on apparent success (feed page loaded), *False* otherwise.
    """
    logger.info("Navigating to LinkedIn login page…")
    try:
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded",
                  timeout=NAVIGATION_TIMEOUT)
    except PlaywrightTimeoutError:
        logger.error("Timed out loading LinkedIn login page.")
        return False

    # Fill credentials.
    try:
        page.fill('input[name="session_key"]', email, timeout=SELECTOR_TIMEOUT)
        page.fill('input[name="session_password"]', password, timeout=SELECTOR_TIMEOUT)
        page.click('button[type="submit"]', timeout=SELECTOR_TIMEOUT)
    except Exception as exc:
        logger.error("Could not interact with login form: %s", exc)
        return False

    # Wait for feed or a redirect that indicates success.
    try:
        page.wait_for_url(
            re.compile(r"linkedin\.com/(feed|mynetwork|jobs)"),
            timeout=NAVIGATION_TIMEOUT,
        )
        logger.info("LinkedIn login successful.")
        return True
    except PlaywrightTimeoutError:
        current = page.url
        logger.error(
            "Login may have failed – unexpected URL after submit: %s", current
        )
        return False


# ---------------------------------------------------------------------------
# Post scraping
# ---------------------------------------------------------------------------


def _extract_post_data(post_locator, page) -> dict | None:
    """
    Extract all available data from a single LinkedIn feed post element.

    Returns a flat dict, or *None* if the post yielded no useful content.
    """
    # --- Expand truncated post text ---
    _try_click_see_more(post_locator)

    # --- Post text (multiple fallback selectors) ---
    post_text = ""
    text_selectors = [
        ".feed-shared-update-v2__description",
        ".feed-shared-text",
        ".update-components-text",
        '[data-test-id="main-feed-activity-card__commentary"]',
        ".feed-shared-update-v2__commentary",
        ".break-words",
    ]
    for sel in text_selectors:
        try:
            loc = post_locator.locator(sel).first
            if loc.count() > 0:
                text = (loc.inner_text() or "").strip()
                if text:
                    post_text = text
                    break
        except Exception:
            pass

    # Fall back to the full inner text of the post card.
    if not post_text:
        try:
            post_text = (post_locator.inner_text() or "").strip()
        except Exception:
            pass

    if not post_text:
        return None

    # --- Author name ---
    author_name = ""
    name_selectors = [
        ".update-components-actor__name span[aria-hidden='true']",
        ".update-components-actor__name",
        ".feed-shared-actor__name",
        ".feed-shared-actor__title",
    ]
    for sel in name_selectors:
        try:
            loc = post_locator.locator(sel).first
            if loc.count() > 0:
                t = (loc.inner_text() or "").strip()
                if t:
                    author_name = t
                    break
        except Exception:
            pass

    # --- Author headline / title ---
    author_title = ""
    title_selectors = [
        ".update-components-actor__description",
        ".feed-shared-actor__description",
        ".update-components-actor__sub-description",
    ]
    for sel in title_selectors:
        try:
            loc = post_locator.locator(sel).first
            if loc.count() > 0:
                t = (loc.inner_text() or "").strip()
                if t:
                    author_title = t
                    break
        except Exception:
            pass

    # --- Author company (extracted from headline if it contains " at ") ---
    author_company = ""
    if " at " in author_title:
        author_company = author_title.split(" at ", 1)[-1].strip()
    elif " | " in author_title:
        # e.g. "Senior Recruiter | Acme Corp"
        author_company = author_title.split(" | ", 1)[-1].strip()

    # --- Author profile URL ---
    author_profile = ""
    profile_selectors = [
        "a.update-components-actor__meta-link",
        "a.feed-shared-actor__container-link",
        "a[href*='/in/']",
        "a[href*='/company/']",
    ]
    for sel in profile_selectors:
        try:
            loc = post_locator.locator(sel).first
            if loc.count() > 0:
                href = (loc.get_attribute("href") or "").strip()
                if not href:
                    continue
                # Make relative paths absolute before further validation.
                if href.startswith("/"):
                    href = "https://www.linkedin.com" + href
                # Only accept URLs whose hostname is linkedin.com (or a subdomain).
                try:
                    hostname = urlparse(href).hostname or ""
                except ValueError:
                    continue
                if (hostname == "linkedin.com" or hostname.endswith(".linkedin.com")):
                    if _is_safe_url(href):
                        author_profile = href
                        break
        except Exception:
            pass

    # --- Post permalink ---
    post_url = ""
    post_url_selectors = [
        "a[href*='/feed/update/']",
        "a[href*='/posts/']",
        "a.app-aware-link[href*='activity']",
    ]
    for sel in post_url_selectors:
        try:
            loc = post_locator.locator(sel).first
            if loc.count() > 0:
                href = (loc.get_attribute("href") or "").strip()
                if href:
                    if href.startswith("/"):
                        href = "https://www.linkedin.com" + href
                    if _is_safe_url(href):
                        post_url = href
                        break
        except Exception:
            pass

    # --- Contact info extracted from post text ---
    emails = _extract_emails(post_text)
    phones = _extract_phones(post_text)
    hr_signal = _has_hr_signal(post_text)

    return {
        "author_name": author_name,
        "author_title": author_title,
        "author_company": author_company,
        "author_profile": author_profile,
        "post_url": post_url,
        "post_text": post_text,
        "email": "; ".join(emails),
        "phone": "; ".join(phones),
        "hr_signal": "yes" if hr_signal else "",
    }


# ---------------------------------------------------------------------------
# Feed scrape pipeline
# ---------------------------------------------------------------------------


def scrape_feed(
    keyword: str,
    email_cred: str,
    password_cred: str,
    num_results: int = 20,
) -> list[dict]:
    """
    Log into LinkedIn, search the feed for *keyword*, and collect up to
    *num_results* post records.

    Parameters
    ----------
    keyword:
        The search term to query on LinkedIn.
    email_cred:
        LinkedIn account e-mail address.
    password_cred:
        LinkedIn account password.
    num_results:
        Maximum number of posts to collect.

    Returns
    -------
    list[dict]
        Flat records ready for CSV output.
    """
    records: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT)

        # --- Log in ---
        if not linkedin_login(page, email_cred, password_cred):
            logger.error("Aborting – LinkedIn login failed.")
            browser.close()
            return records

        # Brief pause after login to let the feed settle.
        page.wait_for_timeout(2_000)

        # --- Navigate to search results (posts / content) ---
        search_url = (
            "https://www.linkedin.com/search/results/content/"
            f"?keywords={quote_plus(keyword)}"
            "&origin=SWITCH_SEARCH_VERTICAL"
        )
        logger.info("Searching LinkedIn feed: %s", search_url)
        try:
            page.goto(search_url, wait_until="domcontentloaded",
                      timeout=NAVIGATION_TIMEOUT)
            page.wait_for_timeout(3_000)
        except PlaywrightTimeoutError:
            logger.error("Timed out loading LinkedIn search page.")
            browser.close()
            return records

        # --- Collect posts by scrolling ---
        seen_texts: set[str] = set()
        scroll_attempts = 0

        logger.info("Collecting posts (target: %d)…", num_results)

        while len(records) < num_results and scroll_attempts < MAX_SCROLL_ATTEMPTS:
            # LinkedIn renders posts in a list of <li> or <div> cards.
            post_card_selectors = [
                "div.feed-shared-update-v2",
                "li.occludable-update",
                "div.occludable-update",
                "div[data-urn]",
            ]

            for card_sel in post_card_selectors:
                cards = page.locator(card_sel).all()
                if cards:
                    break

            for card in cards:
                if len(records) >= num_results:
                    break

                # Use first 120 chars of inner text as a dedup key.
                try:
                    preview = (card.inner_text() or "")[:120]
                except Exception:
                    preview = ""

                if not preview or preview in seen_texts:
                    continue
                seen_texts.add(preview)

                data = _extract_post_data(card, page)
                if not data:
                    continue

                data["keyword"] = keyword
                data["lead_score"] = score_lead(data)
                records.append(data)
                logger.info(
                    "[%d] author=%r  emails=%r  hr=%s",
                    len(records),
                    data["author_name"],
                    data["email"],
                    data["hr_signal"],
                )

            if len(records) >= num_results:
                break

            # Scroll down to trigger lazy-loading of more posts.
            page.evaluate("window.scrollBy(0, 1200)")
            time.sleep(SCROLL_PAUSE)
            scroll_attempts += 1

        browser.close()

    logger.info(
        "Collected %d post(s). Emails found in %d post(s).",
        len(records),
        sum(1 for r in records if r.get("email")),
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
        Rows produced by :func:`scrape_feed`.
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
        "keyword",
        "post_url",
        "author_name",
        "author_title",
        "author_company",
        "author_profile",
        "email",
        "phone",
        "hr_signal",
        "post_text",
        "lead_score",
    ]

    with open(resolved, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(
            csvfile, fieldnames=fieldnames, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(records)

    logger.info("Results saved to '%s' (%d row(s)).", resolved, len(records))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "LinkedIn Feed Outreach Lead Generator – search the LinkedIn feed "
            "for a keyword, scrape full post descriptions, extract contact info "
            "and HR signals, score each lead, and save to CSV."
        )
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("LINKEDIN_EMAIL", ""),
        help=(
            "LinkedIn account e-mail. "
            "Defaults to the LINKEDIN_EMAIL environment variable."
        ),
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("LINKEDIN_PASSWORD", ""),
        help=(
            "LinkedIn account password. "
            "Defaults to the LINKEDIN_PASSWORD environment variable."
        ),
    )
    parser.add_argument(
        "--keyword",
        required=True,
        help="Search keyword or phrase (e.g. 'HR manager hiring London').",
    )
    parser.add_argument(
        "--results",
        type=int,
        default=20,
        metavar="N",
        help="Maximum number of posts to collect (default: 20).",
    )
    parser.add_argument(
        "--output",
        default="linkedin_leads.csv",
        metavar="FILE",
        help="Path to the output CSV file (default: linkedin_leads.csv).",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.email or not args.password:
        parser.error(
            "LinkedIn credentials are required.  Supply them via "
            "--email / --password or the LINKEDIN_EMAIL / LINKEDIN_PASSWORD "
            "environment variables."
        )

    records = scrape_feed(
        keyword=args.keyword,
        email_cred=args.email,
        password_cred=args.password,
        num_results=args.results,
    )
    save_to_csv(records, args.output)


if __name__ == "__main__":
    main()
