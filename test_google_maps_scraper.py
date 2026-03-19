"""
Unit tests for google_maps_scraper.py
All external I/O (Playwright browser, HTTP requests) is mocked.
"""

import csv
import os
import shutil
import tempfile
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch, mock_open, call
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
import google_maps_scraper as gms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_HTML_WITH_CONTACT = """
<html>
<head>
  <title>Acme Agency – Digital Marketing London</title>
  <meta name="description" content="Award-winning digital agency.">
</head>
<body>
  <a href="mailto:hello@acme.co.uk">hello@acme.co.uk</a>
  <a href="mailto:info@acme.co.uk">Contact us</a>
  <a href="/contact">Contact</a>
  <a href="tel:+442071234567">+44 20 7123 4567</a>
  <a href="https://www.linkedin.com/company/acme">LinkedIn</a>
  <a href="https://twitter.com/acme">Twitter</a>
  <a href="https://www.facebook.com/acme">Facebook</a>
  <a href="https://www.instagram.com/acme">Instagram</a>
  <p>You can also reach us at support@acme.co.uk or call us.</p>
</body>
</html>
"""

SAMPLE_HTML_MINIMAL = """
<html><body>
  <p>No contact info here. Email: user@example.com</p>
  <a href="https://www.facebook.com/example">Facebook</a>
</body></html>
"""

SAMPLE_HTML_HTTP_ONLY = """
<html>
<head></head>
<body><p>Plain site, no meta, no contact link.</p></body>
</html>
"""


# ---------------------------------------------------------------------------
# _is_safe_url
# ---------------------------------------------------------------------------

class TestIsSafeUrl(unittest.TestCase):

    def test_https_allowed(self):
        self.assertTrue(gms._is_safe_url("https://example.com/path"))

    def test_http_allowed(self):
        self.assertTrue(gms._is_safe_url("http://example.com/path"))

    def test_ftp_blocked(self):
        self.assertFalse(gms._is_safe_url("ftp://example.com"))

    def test_file_scheme_blocked(self):
        self.assertFalse(gms._is_safe_url("file:///etc/passwd"))

    def test_localhost_blocked(self):
        self.assertFalse(gms._is_safe_url("http://localhost/admin"))

    def test_127_loopback_blocked(self):
        self.assertFalse(gms._is_safe_url("http://127.0.0.1/"))

    def test_private_10_blocked(self):
        self.assertFalse(gms._is_safe_url("http://10.0.0.1/"))

    def test_private_192168_blocked(self):
        self.assertFalse(gms._is_safe_url("http://192.168.1.1/"))

    def test_private_172_16_blocked(self):
        self.assertFalse(gms._is_safe_url("http://172.16.0.1/"))

    def test_private_172_31_blocked(self):
        self.assertFalse(gms._is_safe_url("http://172.31.255.255/"))

    def test_link_local_blocked(self):
        self.assertFalse(gms._is_safe_url("http://169.254.0.1/"))

    def test_ipv6_loopback_blocked(self):
        self.assertFalse(gms._is_safe_url("http://::1/"))

    def test_empty_string_blocked(self):
        self.assertFalse(gms._is_safe_url(""))

    def test_no_hostname_blocked(self):
        self.assertFalse(gms._is_safe_url("https:///path"))


# ---------------------------------------------------------------------------
# _extract_emails_from_soup
# ---------------------------------------------------------------------------

class TestExtractEmails(unittest.TestCase):

    def _soup(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser")

    def test_extracts_mailto_links(self):
        soup = self._soup('<a href="mailto:hello@acme.co.uk">email</a>')
        emails = gms._extract_emails_from_soup(soup)
        self.assertIn("hello@acme.co.uk", emails)

    def test_extracts_emails_from_visible_text(self):
        soup = self._soup("<p>Contact support@example.com today.</p>")
        emails = gms._extract_emails_from_soup(soup)
        self.assertIn("support@example.com", emails)

    def test_multiple_emails(self):
        soup = self._soup(SAMPLE_HTML_WITH_CONTACT)
        emails = gms._extract_emails_from_soup(soup)
        self.assertIn("hello@acme.co.uk", emails)
        self.assertIn("info@acme.co.uk", emails)
        self.assertIn("support@acme.co.uk", emails)
        self.assertEqual(len(emails), 3)

    def test_no_emails_returns_empty_set(self):
        soup = self._soup("<p>No email here.</p>")
        self.assertEqual(gms._extract_emails_from_soup(soup), set())

    def test_case_normalised_to_lowercase(self):
        soup = self._soup("<p>UPPER@Example.COM</p>")
        emails = gms._extract_emails_from_soup(soup)
        self.assertIn("upper@example.com", emails)


# ---------------------------------------------------------------------------
# _extract_phone_from_soup
# ---------------------------------------------------------------------------

class TestExtractPhone(unittest.TestCase):

    def _soup(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser")

    def test_tel_link_preferred(self):
        soup = self._soup('<a href="tel:+442071234567">Call us</a>')
        self.assertEqual(gms._extract_phone_from_soup(soup), "+442071234567")

    def test_regex_fallback(self):
        soup = self._soup("<p>Call +44 20 7123 4567 now.</p>")
        phone = gms._extract_phone_from_soup(soup)
        self.assertIn("+44", phone)

    def test_no_phone_returns_empty(self):
        soup = self._soup("<p>No phone here.</p>")
        self.assertEqual(gms._extract_phone_from_soup(soup), "")


# ---------------------------------------------------------------------------
# _find_contact_page_url
# ---------------------------------------------------------------------------

class TestFindContactPageUrl(unittest.TestCase):

    def _soup(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser")

    def test_finds_contact_link_by_text(self):
        soup = self._soup('<a href="/contact-us">Contact</a>')
        url = gms._find_contact_page_url("https://example.com", soup)
        self.assertEqual(url, "https://example.com/contact-us")

    def test_finds_contact_link_by_href(self):
        soup = self._soup('<a href="/pages/contact">Get in touch</a>')
        url = gms._find_contact_page_url("https://example.com", soup)
        self.assertEqual(url, "https://example.com/pages/contact")

    def test_returns_empty_when_none(self):
        soup = self._soup("<p>No links here.</p>")
        url = gms._find_contact_page_url("https://example.com", soup)
        self.assertEqual(url, "")

    def test_unsafe_absolute_href_ignored(self):
        soup = self._soup('<a href="http://192.168.1.1/contact">Contact</a>')
        url = gms._find_contact_page_url("https://example.com", soup)
        self.assertEqual(url, "")


# ---------------------------------------------------------------------------
# _extract_social_links
# ---------------------------------------------------------------------------

class TestExtractSocialLinks(unittest.TestCase):

    def _soup(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser")

    def test_all_social_found(self):
        soup = self._soup(SAMPLE_HTML_WITH_CONTACT)
        social = gms._extract_social_links(soup)
        self.assertIn("linkedin.com", social["linkedin"])
        self.assertIn("twitter.com", social["twitter"])
        self.assertIn("facebook.com", social["facebook"])
        self.assertIn("instagram.com", social["instagram"])

    def test_x_com_twitter_alias(self):
        soup = self._soup('<a href="https://x.com/acme">X</a>')
        social = gms._extract_social_links(soup)
        self.assertIn("x.com", social["twitter"])

    def test_missing_social_returns_empty_string(self):
        soup = self._soup("<p>No social links.</p>")
        social = gms._extract_social_links(soup)
        self.assertEqual(social["linkedin"], "")
        self.assertEqual(social["twitter"], "")

    def test_only_first_link_per_platform(self):
        soup = self._soup(
            '<a href="https://linkedin.com/company/a">A</a>'
            '<a href="https://linkedin.com/company/b">B</a>'
        )
        social = gms._extract_social_links(soup)
        self.assertIn("/company/a", social["linkedin"])


# ---------------------------------------------------------------------------
# analyze_website
# ---------------------------------------------------------------------------

class TestAnalyzeWebsite(unittest.TestCase):

    def test_https_no_ssl_issue(self):
        issues = gms.analyze_website("https://example.com", SAMPLE_HTML_WITH_CONTACT)
        self.assertNotIn("No SSL (site not secure)", issues)

    def test_http_flags_no_ssl(self):
        issues = gms.analyze_website("http://example.com", SAMPLE_HTML_WITH_CONTACT)
        self.assertIn("No SSL (site not secure)", issues)

    def test_missing_title_flagged(self):
        issues = gms.analyze_website("https://example.com", SAMPLE_HTML_HTTP_ONLY)
        self.assertIn("Missing page title", issues)

    def test_missing_meta_description_flagged(self):
        issues = gms.analyze_website("https://example.com", SAMPLE_HTML_HTTP_ONLY)
        self.assertIn("Missing meta description", issues)

    def test_no_contact_link_flagged(self):
        issues = gms.analyze_website("https://example.com", SAMPLE_HTML_MINIMAL)
        self.assertIn("No contact page link found", issues)

    def test_fully_good_site_has_no_issues(self):
        issues = gms.analyze_website("https://example.com", SAMPLE_HTML_WITH_CONTACT)
        self.assertEqual(issues, [])


# ---------------------------------------------------------------------------
# score_lead
# ---------------------------------------------------------------------------

class TestScoreLead(unittest.TestCase):

    def test_full_data_max_score(self):
        data = {
            "emails": {"a@b.com"},
            "phone": "+44123",
            "website": "https://example.com",
            "linkedin": "https://linkedin.com/company/x",
            "twitter": "https://twitter.com/x",
            "facebook": "",
            "instagram": "",
            "issues": [],
        }
        # 3+2+1+1+1 = 8
        self.assertEqual(gms.score_lead(data), 8)

    def test_issues_deduct_score(self):
        data = {
            "emails": set(),
            "phone": "",
            "website": "",
            "linkedin": "",
            "twitter": "",
            "facebook": "",
            "instagram": "",
            "issues": ["No SSL", "Missing title", "Missing meta"],
        }
        self.assertEqual(gms.score_lead(data), -3)

    def test_email_only(self):
        data = {
            "emails": {"a@b.com"},
            "phone": "",
            "website": "",
            "linkedin": "",
            "twitter": "",
            "facebook": "",
            "instagram": "",
            "issues": [],
        }
        self.assertEqual(gms.score_lead(data), 3)

    def test_empty_data_scores_zero(self):
        self.assertEqual(gms.score_lead({}), 0)

    def test_any_social_counts_once(self):
        data = {
            "emails": set(),
            "phone": "",
            "website": "",
            "linkedin": "",
            "twitter": "https://twitter.com/x",
            "facebook": "https://facebook.com/x",
            "instagram": "https://instagram.com/x",
            "issues": [],
        }
        # Only 1 point for "any social" even if multiple platforms present
        self.assertEqual(gms.score_lead(data), 1)


# ---------------------------------------------------------------------------
# enrich_from_website  (HTTP fully mocked)
# ---------------------------------------------------------------------------

class TestEnrichFromWebsite(unittest.TestCase):

    def _make_response(self, text, status=200):
        resp = MagicMock()
        resp.text = text
        resp.status_code = status
        resp.raise_for_status = MagicMock()
        return resp

    def test_extracts_all_fields(self):
        session = MagicMock()
        session.get.return_value = self._make_response(SAMPLE_HTML_WITH_CONTACT)

        result = gms.enrich_from_website("https://acme.co.uk", session)

        self.assertIn("hello@acme.co.uk", result["emails"])
        self.assertIn("info@acme.co.uk", result["emails"])
        self.assertTrue(result["phone"])
        self.assertIn("linkedin.com", result["linkedin"])
        self.assertIn("twitter.com", result["twitter"])
        self.assertIn("facebook.com", result["facebook"])
        self.assertIn("instagram.com", result["instagram"])

    def test_visits_contact_page_for_extra_emails(self):
        homepage_html = """
        <html><head><title>T</title><meta name="description" content="D"></head>
        <body><a href="/contact">Contact</a></body></html>
        """
        contact_html = """
        <html><body>
        <a href="mailto:contact@acme.co.uk">Email us</a>
        </body></html>
        """
        session = MagicMock()
        session.get.side_effect = [
            self._make_response(homepage_html),
            self._make_response(contact_html),
        ]
        result = gms.enrich_from_website("https://acme.co.uk", session)
        self.assertIn("contact@acme.co.uk", result["emails"])

    def test_unsafe_url_returns_empty(self):
        session = MagicMock()
        result = gms.enrich_from_website("http://192.168.1.1/", session)
        session.get.assert_not_called()
        self.assertEqual(result["emails"], set())

    def test_request_exception_returns_empty(self):
        import requests as req
        session = MagicMock()
        session.get.side_effect = req.RequestException("connection error")
        result = gms.enrich_from_website("https://broken.com", session)
        self.assertEqual(result["emails"], set())

    def test_too_many_redirects_returns_empty(self):
        import requests as req
        session = MagicMock()
        session.get.side_effect = req.TooManyRedirects()
        result = gms.enrich_from_website("https://redirected.com", session)
        self.assertEqual(result["emails"], set())


# ---------------------------------------------------------------------------
# save_to_csv
# ---------------------------------------------------------------------------

class TestSaveToCsv(unittest.TestCase):

    def test_writes_correct_headers_and_rows(self):
        records = [
            {
                "keyword": "test", "name": "Acme", "address": "London",
                "phone": "+441234", "website": "https://acme.com",
                "rating": "4.5", "reviews": "200", "category": "Agency",
                "email": "hello@acme.com", "linkedin": "", "twitter": "",
                "facebook": "", "instagram": "", "contact_page": "",
                "issues": "", "lead_score": 5,
            }
        ]
        with tempfile.NamedTemporaryFile(
            suffix=".csv", delete=False, mode="w"
        ) as tmp:
            output = tmp.name

        try:
            gms.save_to_csv(records, output)
            self.assertTrue(os.path.exists(output))
            with open(output, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "Acme")
            self.assertEqual(rows[0]["email"], "hello@acme.com")
            self.assertEqual(rows[0]["lead_score"], "5")
        finally:
            os.remove(output)

    def test_multiple_emails_produce_multiple_rows(self):
        """
        The scraper emits one row per email; test that save_to_csv
        faithfully writes all rows.
        """
        records = [
            {k: "" for k in [
                "keyword","name","address","phone","website","rating",
                "reviews","category","email","linkedin","twitter",
                "facebook","instagram","contact_page","issues","lead_score",
            ]}
            | {"email": "a@x.com", "lead_score": 3},
            {k: "" for k in [
                "keyword","name","address","phone","website","rating",
                "reviews","category","email","linkedin","twitter",
                "facebook","instagram","contact_page","issues","lead_score",
            ]}
            | {"email": "b@x.com", "lead_score": 3},
        ]
        with tempfile.NamedTemporaryFile(
            suffix=".csv", delete=False, mode="w"
        ) as tmp:
            output = tmp.name

        try:
            gms.save_to_csv(records, output)
            with open(output, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 2)
            emails = {r["email"] for r in rows}
            self.assertEqual(emails, {"a@x.com", "b@x.com"})
        finally:
            os.remove(output)

    def test_raises_on_directory_path(self):
        tmp_dir = tempfile.mkdtemp()
        try:
            with self.assertRaises(ValueError):
                gms.save_to_csv([], tmp_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_creates_parent_directories(self):
        tmp_dir = tempfile.mkdtemp()
        output = os.path.join(tmp_dir, "nested", "leads.csv")
        try:
            gms.save_to_csv([], output)
            self.assertTrue(os.path.exists(output))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# scrape_google_maps  (Playwright fully mocked via patch)
# ---------------------------------------------------------------------------

class TestScrapeGoogleMaps(unittest.TestCase):
    """
    Verify the scrape pipeline by mocking Playwright and HTTP entirely.
    50 simulated listings are produced, matching the target lead count.
    """

    def _build_mock_listing(self, idx: int):
        """Return a mock Playwright page representing a single business listing."""
        page = MagicMock()
        page.wait_for_timeout = MagicMock()

        # Simulate safe_text / safe_attr via locator chain
        def locator_side_effect(selector):
            loc = MagicMock()
            el = MagicMock()
            el.inner_text.return_value = {
                "h1": f"Business {idx}",
                "h1.DUwDvf": f"Business {idx}",
                "h1[class*=\"fontHeadlineLarge\"]": f"Business {idx}",
                "h1[class*=\"section-hero-header-title\"]": f"Business {idx}",
                "button[jsaction*=\"category\"]": "Digital Agency",
                "button.DkEaL": "Digital Agency",
                '[data-item-id="category"] span': "Digital Agency",
                'button[data-item-id="address"]': f"{idx} High Street, London",
                '[data-tooltip="Copy address"] span': f"{idx} High Street, London",
                'div[data-item-id*="address"] span': f"{idx} High Street, London",
                'button[data-item-id*="phone"]': f"+44207000{idx:04d}",
                '[data-tooltip="Copy phone number"] span': f"+44207000{idx:04d}",
                'span[aria-hidden="true"].ceNzKf': "4.5",
                'div.F7nice span[aria-hidden="true"]': "4.5",
                'span.MW4etd': "4.5",
                'span[aria-label*="review" i]': f"({idx * 10})",
                'button[jsaction*="review"] span': f"({idx * 10})",
                'span.UY7F9': f"({idx * 10})",
            }.get(selector, "")
            el.get_attribute.return_value = {
                'a[data-item-id="authority"]': f"https://business{idx}.example.com",
                'a[href*="//"][aria-label*="website" i]': f"https://business{idx}.example.com",
                'a[href^="tel:"]': f"tel:+44207000{idx:04d}",
            }.get(selector, "")
            el.wait_for = MagicMock()
            loc.first = el
            return loc

        page.locator.side_effect = locator_side_effect
        return page

    @patch("google_maps_scraper.enrich_from_website")
    @patch("google_maps_scraper.sync_playwright")
    @patch("google_maps_scraper._create_session")
    def test_collects_50_leads(self, mock_session, mock_playwright, mock_enrich):
        """
        Verify that scrape_google_maps returns exactly 50 records when
        50 distinct listing URLs are discovered on the mock results page.
        """
        # --- Mock HTTP session (used for website enrichment) ---
        mock_session.return_value = MagicMock()

        # --- Mock enrichment: every website yields one email ---
        mock_enrich.return_value = {
            "emails": {"contact@example.com"},
            "phone": "",
            "linkedin": "https://linkedin.com/company/biz",
            "twitter": "",
            "facebook": "",
            "instagram": "",
            "contact_page": "https://example.com/contact",
            "issues": [],
        }

        # --- Build 50 fake listing URLs ---
        listing_urls = [
            f"https://www.google.com/maps/place/Business+{i}/@51.5,-0.1,17z/data=x"
            for i in range(1, 51)
        ]

        # --- Mock Playwright context manager chain ---
        mock_pw_cm = MagicMock()
        mock_playwright.return_value.__enter__.return_value = mock_pw_cm

        mock_browser = MagicMock()
        mock_pw_cm.chromium.launch.return_value = mock_browser

        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context

        # Two distinct page objects: results page + per-listing page.
        results_page = MagicMock()
        results_page.wait_for_timeout = MagicMock()

        # results_page.goto does nothing (no error = success)
        results_page.goto = MagicMock()

        # Consent button: not visible
        results_page.get_by_role.return_value.is_visible.return_value = False

        # wait_for_selector succeeds (feed found)
        results_page.wait_for_selector = MagicMock()

        # locator('a[href*="/maps/place/"]').all() returns link mocks
        def results_locator(selector):
            loc = MagicMock()
            if selector == 'a[href*="/maps/place/"]':
                link_mocks = []
                for url in listing_urls:
                    lm = MagicMock()
                    lm.get_attribute.return_value = url
                    link_mocks.append(lm)
                loc.all.return_value = link_mocks
            elif selector == 'div[role="feed"]':
                feed = MagicMock()
                feed.evaluate = MagicMock()
                loc.first = feed
            else:
                loc.all.return_value = []
            return loc

        results_page.locator.side_effect = results_locator
        results_page.evaluate = MagicMock()

        # Detail pages (one per listing) — delegate to _build_mock_listing
        detail_pages = [self._build_mock_listing(i) for i in range(1, 51)]

        # new_page() returns results_page first, then one detail page per listing
        mock_context.new_page.return_value = results_page

        # Each listing gets its own goto on results_page (we reuse the same page)
        # We patch goto so detail scraping uses _build_mock_listing responses
        call_count = {"n": 0}
        def goto_side_effect(url, **kwargs):
            pass  # always succeeds silently

        results_page.goto.side_effect = goto_side_effect

        # Override locator for detail pages by swapping side_effect after first goto
        goto_calls = {"n": 0}
        current_detail = {"page": None}

        original_goto = results_page.goto

        def patched_goto(url, **kwargs):
            if "/maps/place/" in url:
                idx = goto_calls["n"] % 50
                detail = detail_pages[idx]
                # Replace locator/wait_for_timeout on results_page for this listing
                results_page.locator.side_effect = detail.locator.side_effect
                results_page.wait_for_timeout = MagicMock()
                goto_calls["n"] += 1
            # Reset locator for results page on search URL
            elif "/maps/search/" in url:
                results_page.locator.side_effect = results_locator

        results_page.goto.side_effect = patched_goto

        with patch("time.sleep"):
            records = gms.scrape_google_maps("digital agency London", num_results=50)

        # Each listing yields 1 email → 1 row each → 50 rows total
        self.assertEqual(len(records), 50)

        # Spot-check fields
        for rec in records:
            self.assertEqual(rec["keyword"], "digital agency London")
            self.assertIn("email", rec)
            self.assertIn("lead_score", rec)
            self.assertIsInstance(rec["lead_score"], int)

    @patch("google_maps_scraper.enrich_from_website")
    @patch("google_maps_scraper.sync_playwright")
    @patch("google_maps_scraper._create_session")
    def test_no_listings_returns_empty(self, mock_session, mock_playwright, mock_enrich):
        """When the results feed yields no links, return an empty list."""
        mock_session.return_value = MagicMock()
        mock_enrich.return_value = {
            "emails": set(), "phone": "", "linkedin": "",
            "twitter": "", "facebook": "", "instagram": "",
            "contact_page": "", "issues": [],
        }

        mock_pw_cm = MagicMock()
        mock_playwright.return_value.__enter__.return_value = mock_pw_cm

        mock_browser = MagicMock()
        mock_pw_cm.chromium.launch.return_value = mock_browser

        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context

        page = MagicMock()
        page.goto = MagicMock()
        page.get_by_role.return_value.is_visible.return_value = False
        page.wait_for_selector = MagicMock()
        page.wait_for_timeout = MagicMock()
        page.evaluate = MagicMock()

        def no_links_locator(selector):
            loc = MagicMock()
            loc.all.return_value = []
            feed = MagicMock()
            feed.evaluate = MagicMock()
            loc.first = feed
            return loc

        page.locator.side_effect = no_links_locator
        mock_context.new_page.return_value = page

        with patch("time.sleep"):
            records = gms.scrape_google_maps("nonexistent query xyz", num_results=50)

        self.assertEqual(records, [])

    @patch("google_maps_scraper.sync_playwright")
    @patch("google_maps_scraper._create_session")
    def test_playwright_timeout_returns_empty(self, mock_session, mock_playwright):
        """A timeout on the initial page load returns an empty list gracefully."""
        from playwright.sync_api import TimeoutError as PwTimeout

        mock_session.return_value = MagicMock()
        mock_pw_cm = MagicMock()
        mock_playwright.return_value.__enter__.return_value = mock_pw_cm

        mock_browser = MagicMock()
        mock_pw_cm.chromium.launch.return_value = mock_browser

        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context

        page = MagicMock()
        page.goto.side_effect = PwTimeout("timeout")
        mock_context.new_page.return_value = page

        with patch("time.sleep"):
            records = gms.scrape_google_maps("any keyword", num_results=50)

        self.assertEqual(records, [])

    @patch("google_maps_scraper.enrich_from_website")
    @patch("google_maps_scraper.sync_playwright")
    @patch("google_maps_scraper._create_session")
    def test_listing_without_website_skips_enrichment(
        self, mock_session, mock_playwright, mock_enrich
    ):
        """Listings without a website URL should skip enrich_from_website."""
        mock_session.return_value = MagicMock()
        mock_enrich.return_value = {
            "emails": set(), "phone": "", "linkedin": "",
            "twitter": "", "facebook": "", "instagram": "",
            "contact_page": "", "issues": [],
        }

        listing_url = "https://www.google.com/maps/place/NoWebsite/@51.5,-0.1,17z"

        mock_pw_cm = MagicMock()
        mock_playwright.return_value.__enter__.return_value = mock_pw_cm
        mock_browser = MagicMock()
        mock_pw_cm.chromium.launch.return_value = mock_browser
        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context

        page = MagicMock()
        page.wait_for_timeout = MagicMock()
        page.get_by_role.return_value.is_visible.return_value = False
        page.wait_for_selector = MagicMock()
        page.evaluate = MagicMock()

        links_returned = {"done": False}

        def locator_side_effect(selector):
            loc = MagicMock()
            if selector == 'a[href*="/maps/place/"]' and not links_returned["done"]:
                lm = MagicMock()
                lm.get_attribute.return_value = listing_url
                loc.all.return_value = [lm]
                links_returned["done"] = True
            elif selector == 'div[role="feed"]':
                feed = MagicMock()
                feed.evaluate = MagicMock()
                loc.first = feed
            else:
                el = MagicMock()
                el.inner_text.return_value = ""
                el.get_attribute.return_value = ""  # No website
                el.wait_for = MagicMock()
                loc.first = el
                loc.all.return_value = []
            return loc

        page.locator.side_effect = locator_side_effect
        mock_context.new_page.return_value = page

        with patch("time.sleep"):
            records = gms.scrape_google_maps("test", num_results=1)

        # enrich_from_website should NOT have been called (no website URL)
        mock_enrich.assert_not_called()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["email"], "")


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------

class TestArgParser(unittest.TestCase):

    def test_required_keyword(self):
        parser = gms.build_arg_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_defaults(self):
        parser = gms.build_arg_parser()
        args = parser.parse_args(["--keyword", "test query"])
        self.assertEqual(args.keyword, "test query")
        self.assertEqual(args.results, 50)
        self.assertEqual(args.output, "leads.csv")

    def test_custom_args(self):
        parser = gms.build_arg_parser()
        args = parser.parse_args([
            "--keyword", "restaurants NYC",
            "--results", "100",
            "--output", "nyc_leads.csv",
        ])
        self.assertEqual(args.keyword, "restaurants NYC")
        self.assertEqual(args.results, 100)
        self.assertEqual(args.output, "nyc_leads.csv")


if __name__ == "__main__":
    unittest.main(verbosity=2)
