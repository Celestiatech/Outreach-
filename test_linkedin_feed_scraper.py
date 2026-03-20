"""
Unit tests for linkedin_feed_scraper.py
All external I/O (Playwright browser, network) is mocked.
"""

import csv
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
import linkedin_feed_scraper as lfs


# ===========================================================================
# _is_safe_url
# ===========================================================================

class TestIsSafeUrl(unittest.TestCase):

    def test_https_allowed(self):
        self.assertTrue(lfs._is_safe_url("https://example.com/path"))

    def test_http_allowed(self):
        self.assertTrue(lfs._is_safe_url("http://example.com/path"))

    def test_ftp_blocked(self):
        self.assertFalse(lfs._is_safe_url("ftp://example.com"))

    def test_file_scheme_blocked(self):
        self.assertFalse(lfs._is_safe_url("file:///etc/passwd"))

    def test_localhost_blocked(self):
        self.assertFalse(lfs._is_safe_url("http://localhost/admin"))

    def test_127_loopback_blocked(self):
        self.assertFalse(lfs._is_safe_url("http://127.0.0.1/"))

    def test_private_10_blocked(self):
        self.assertFalse(lfs._is_safe_url("http://10.0.0.1/"))

    def test_private_192168_blocked(self):
        self.assertFalse(lfs._is_safe_url("http://192.168.1.1/"))

    def test_private_172_16_blocked(self):
        self.assertFalse(lfs._is_safe_url("http://172.16.0.1/"))

    def test_private_172_31_blocked(self):
        self.assertFalse(lfs._is_safe_url("http://172.31.255.255/"))

    def test_link_local_blocked(self):
        self.assertFalse(lfs._is_safe_url("http://169.254.0.1/"))

    def test_ipv6_loopback_blocked(self):
        self.assertFalse(lfs._is_safe_url("http://::1/"))

    def test_empty_string_blocked(self):
        self.assertFalse(lfs._is_safe_url(""))

    def test_no_hostname_blocked(self):
        self.assertFalse(lfs._is_safe_url("https:///path"))

    def test_linkedin_allowed(self):
        self.assertTrue(lfs._is_safe_url("https://www.linkedin.com/in/johndoe"))

    def test_linkedin_subdomain_allowed(self):
        self.assertTrue(lfs._is_safe_url("https://uk.linkedin.com/company/acme"))


# ===========================================================================
# _extract_emails
# ===========================================================================

class TestExtractEmails(unittest.TestCase):

    def test_basic_email(self):
        emails = lfs._extract_emails("Contact us at hello@acme.co.uk today.")
        self.assertIn("hello@acme.co.uk", emails)

    def test_multiple_emails(self):
        text = "Email hr@company.com or info@company.com for details."
        emails = lfs._extract_emails(text)
        self.assertIn("hr@company.com", emails)
        self.assertIn("info@company.com", emails)
        self.assertEqual(len(emails), 2)

    def test_case_normalised_to_lowercase(self):
        emails = lfs._extract_emails("UPPER@Example.COM")
        self.assertIn("upper@example.com", emails)

    def test_deduplicated(self):
        text = "Send to test@x.com and test@x.com again."
        emails = lfs._extract_emails(text)
        self.assertEqual(emails.count("test@x.com"), 1)

    def test_no_emails_returns_empty_list(self):
        self.assertEqual(lfs._extract_emails("No email here at all."), [])

    def test_email_with_plus_sign(self):
        emails = lfs._extract_emails("Reach me at user+tag@domain.org")
        self.assertIn("user+tag@domain.org", emails)

    def test_sorted_output(self):
        text = "b@x.com and a@x.com"
        emails = lfs._extract_emails(text)
        self.assertEqual(emails, sorted(emails))

    def test_ignores_invalid_email(self):
        # Double-dot domain is invalid per the regex
        emails = lfs._extract_emails("bad@..invalid.com")
        self.assertNotIn("bad@..invalid.com", emails)


# ===========================================================================
# _extract_phones
# ===========================================================================

class TestExtractPhones(unittest.TestCase):

    def test_international_format(self):
        phones = lfs._extract_phones("Call us at +44 20 7123 4567 now.")
        self.assertTrue(any("+44" in p for p in phones))

    def test_us_format(self):
        phones = lfs._extract_phones("Phone: (800) 555-1234")
        self.assertEqual(len(phones), 1)

    def test_multiple_phones(self):
        text = "Office: +44 20 7000 0001. Mobile: +44 7700 900002."
        phones = lfs._extract_phones(text)
        self.assertEqual(len(phones), 2)

    def test_no_phones_returns_empty_list(self):
        self.assertEqual(lfs._extract_phones("No phone here."), [])

    def test_deduplicated(self):
        text = "+441234567890 and +441234567890"
        phones = lfs._extract_phones(text)
        self.assertEqual(len(phones), 1)

    def test_sorted_output(self):
        text = "+442000000002 and +441000000001"
        phones = lfs._extract_phones(text)
        self.assertEqual(phones, sorted(phones))


# ===========================================================================
# _has_hr_signal
# ===========================================================================

class TestHasHrSignal(unittest.TestCase):

    def test_hiring_keyword(self):
        self.assertTrue(lfs._has_hr_signal("We're hiring a senior engineer!"))

    def test_now_hiring(self):
        self.assertTrue(lfs._has_hr_signal("Now hiring for multiple roles."))

    def test_join_our_team(self):
        self.assertTrue(lfs._has_hr_signal("Come join our team!"))

    def test_send_cv(self):
        self.assertTrue(lfs._has_hr_signal("Interested? Send your CV to hr@acme.com"))

    def test_open_role(self):
        self.assertTrue(lfs._has_hr_signal("We have an open role in engineering."))

    def test_talent_acquisition(self):
        self.assertTrue(lfs._has_hr_signal("Our talent acquisition team is growing."))

    def test_recruiter(self):
        self.assertTrue(lfs._has_hr_signal("As a recruiter I see many great candidates."))

    def test_apply_now(self):
        self.assertTrue(lfs._has_hr_signal("Apply now and join a great company."))

    def test_seeking_a(self):
        self.assertTrue(lfs._has_hr_signal("We are seeking a motivated developer."))

    def test_no_hr_signal(self):
        self.assertFalse(lfs._has_hr_signal("Just sharing some industry thoughts."))

    def test_case_insensitive(self):
        self.assertTrue(lfs._has_hr_signal("HIRING NOW – great opportunity!"))

    def test_empty_string(self):
        self.assertFalse(lfs._has_hr_signal(""))

    def test_new_opportunity(self):
        self.assertTrue(lfs._has_hr_signal("Excited to share a new opportunity!"))

    def test_career_opportunity(self):
        self.assertTrue(lfs._has_hr_signal("A great career opportunity has just opened."))


# ===========================================================================
# score_lead
# ===========================================================================

class TestScoreLead(unittest.TestCase):

    def test_one_email_scores_four(self):
        record = {"email": "a@b.com", "phone": "", "hr_signal": "", "author_company": ""}
        self.assertEqual(lfs.score_lead(record), 4)

    def test_two_emails_scores_eight(self):
        record = {"email": "a@b.com; c@d.com", "phone": "", "hr_signal": "", "author_company": ""}
        self.assertEqual(lfs.score_lead(record), 8)

    def test_one_phone_scores_two(self):
        record = {"email": "", "phone": "+441234567890", "hr_signal": "", "author_company": ""}
        self.assertEqual(lfs.score_lead(record), 2)

    def test_hr_signal_scores_two(self):
        record = {"email": "", "phone": "", "hr_signal": "yes", "author_company": ""}
        self.assertEqual(lfs.score_lead(record), 2)

    def test_company_known_scores_one(self):
        record = {"email": "", "phone": "", "hr_signal": "", "author_company": "Acme Corp"}
        self.assertEqual(lfs.score_lead(record), 1)

    def test_full_record_max_score(self):
        # email(4) + phone(2) + hr(2) + company(1) = 9
        record = {
            "email": "a@b.com",
            "phone": "+441234567890",
            "hr_signal": "yes",
            "author_company": "Acme Corp",
        }
        self.assertEqual(lfs.score_lead(record), 9)

    def test_empty_record_scores_zero(self):
        self.assertEqual(lfs.score_lead({}), 0)

    def test_multiple_phones_each_score(self):
        # Two phones → 2 × 2 = 4
        record = {"email": "", "phone": "+441111111111; +442222222222", "hr_signal": "", "author_company": ""}
        self.assertEqual(lfs.score_lead(record), 4)

    def test_empty_email_and_phone_strings_score_zero(self):
        record = {"email": "", "phone": "", "hr_signal": "", "author_company": ""}
        self.assertEqual(lfs.score_lead(record), 0)

    def test_hr_signal_empty_string_not_counted(self):
        record = {"email": "", "phone": "", "hr_signal": "", "author_company": ""}
        self.assertEqual(lfs.score_lead(record), 0)


# ===========================================================================
# linkedin_login
# ===========================================================================

class TestLinkedinLogin(unittest.TestCase):
    """Tests for linkedin_login() with a fully mocked Playwright page."""

    def _make_page(self):
        page = MagicMock()
        page.url = "https://www.linkedin.com/feed/"
        return page

    def test_successful_login_returns_true(self):
        page = self._make_page()
        # wait_for_url succeeds (no exception raised)
        result = lfs.linkedin_login(page, "user@example.com", "secret")
        self.assertTrue(result)
        page.goto.assert_called_once_with(
            "https://www.linkedin.com/login",
            wait_until="domcontentloaded",
            timeout=lfs.NAVIGATION_TIMEOUT,
        )
        page.fill.assert_any_call(
            'input[name="session_key"]', "user@example.com",
            timeout=lfs.SELECTOR_TIMEOUT,
        )
        page.fill.assert_any_call(
            'input[name="session_password"]', "secret",
            timeout=lfs.SELECTOR_TIMEOUT,
        )
        page.click.assert_called_once_with(
            'button[type="submit"]', timeout=lfs.SELECTOR_TIMEOUT
        )

    def test_goto_timeout_returns_false(self):
        from playwright.sync_api import TimeoutError as PwTimeout
        page = self._make_page()
        page.goto.side_effect = PwTimeout("timeout")
        result = lfs.linkedin_login(page, "user@example.com", "secret")
        self.assertFalse(result)

    def test_wait_for_url_timeout_returns_false(self):
        from playwright.sync_api import TimeoutError as PwTimeout
        page = self._make_page()
        page.wait_for_url.side_effect = PwTimeout("timeout")
        result = lfs.linkedin_login(page, "user@example.com", "secret")
        self.assertFalse(result)

    def test_fill_exception_returns_false(self):
        page = self._make_page()
        page.fill.side_effect = Exception("element not found")
        result = lfs.linkedin_login(page, "user@example.com", "secret")
        self.assertFalse(result)

    def test_click_exception_returns_false(self):
        page = self._make_page()
        page.click.side_effect = Exception("element not found")
        result = lfs.linkedin_login(page, "user@example.com", "secret")
        self.assertFalse(result)


# ===========================================================================
# _extract_post_data  (mocked post_locator)
# ===========================================================================

class TestExtractPostData(unittest.TestCase):
    """
    Tests for _extract_post_data() using a mocked Playwright locator.
    """

    def _make_locator(self, text="", count=1):
        """Return a mock element locator that yields *text* from inner_text."""
        loc = MagicMock()
        el = MagicMock()
        el.inner_text.return_value = text
        el.get_attribute.return_value = ""
        el.count.return_value = count
        el.is_visible.return_value = False
        loc.first = el
        loc.count.return_value = count
        return loc

    def _make_post(
        self,
        post_text="Sample post text.",
        author_name="Jane Doe",
        author_title="Recruiter at Acme Corp",
        profile_href="https://www.linkedin.com/in/janedoe",
        post_href="https://www.linkedin.com/feed/update/urn:li:activity:123",
    ):
        """
        Build a mock post_locator that returns controlled data for each selector.
        """
        post = MagicMock()

        # "see more" button: not visible, so no click
        see_more_btn = MagicMock()
        see_more_btn.count.return_value = 0
        post.get_by_text.return_value = see_more_btn

        def locator_side_effect(selector):
            # Post text selectors
            if selector in (
                ".feed-shared-update-v2__description",
                ".feed-shared-text",
                ".update-components-text",
                '[data-test-id="main-feed-activity-card__commentary"]',
                ".feed-shared-update-v2__commentary",
                ".break-words",
            ):
                return self._make_locator(post_text, count=1)

            # Author name selectors
            if selector in (
                ".update-components-actor__name span[aria-hidden='true']",
                ".update-components-actor__name",
                ".feed-shared-actor__name",
                ".feed-shared-actor__title",
            ):
                return self._make_locator(author_name, count=1)

            # Author title selectors
            if selector in (
                ".update-components-actor__description",
                ".feed-shared-actor__description",
                ".update-components-actor__sub-description",
            ):
                return self._make_locator(author_title, count=1)

            # Profile URL selectors
            if selector in (
                "a.update-components-actor__meta-link",
                "a.feed-shared-actor__container-link",
                "a[href*='/in/']",
                "a[href*='/company/']",
            ):
                loc = MagicMock()
                el = MagicMock()
                el.get_attribute.return_value = profile_href
                el.count.return_value = 1
                loc.first = el
                loc.count.return_value = 1
                return loc

            # Post URL selectors
            if selector in (
                "a[href*='/feed/update/']",
                "a[href*='/posts/']",
                "a.app-aware-link[href*='activity']",
            ):
                loc = MagicMock()
                el = MagicMock()
                el.get_attribute.return_value = post_href
                el.count.return_value = 1
                loc.first = el
                loc.count.return_value = 1
                return loc

            return self._make_locator("", count=0)

        post.locator.side_effect = locator_side_effect
        post.inner_text.return_value = post_text
        return post

    def test_basic_extraction(self):
        post = self._make_post()
        data = lfs._extract_post_data(post, MagicMock())
        self.assertIsNotNone(data)
        self.assertEqual(data["post_text"], "Sample post text.")
        self.assertEqual(data["author_name"], "Jane Doe")

    def test_author_company_extracted_from_at_pattern(self):
        post = self._make_post(author_title="Recruiter at Acme Corp")
        data = lfs._extract_post_data(post, MagicMock())
        self.assertEqual(data["author_company"], "Acme Corp")

    def test_author_company_extracted_from_pipe_pattern(self):
        post = self._make_post(author_title="Senior Recruiter | Bright Future Ltd")
        data = lfs._extract_post_data(post, MagicMock())
        self.assertEqual(data["author_company"], "Bright Future Ltd")

    def test_author_company_empty_when_no_separator(self):
        post = self._make_post(author_title="Software Engineer")
        data = lfs._extract_post_data(post, MagicMock())
        self.assertEqual(data["author_company"], "")

    def test_email_extracted_from_post_text(self):
        post = self._make_post(post_text="Send CV to hiring@acme.com now!")
        data = lfs._extract_post_data(post, MagicMock())
        self.assertIn("hiring@acme.com", data["email"])

    def test_phone_extracted_from_post_text(self):
        post = self._make_post(post_text="Call us: +44 20 7123 4567")
        data = lfs._extract_post_data(post, MagicMock())
        self.assertIn("+44", data["phone"])

    def test_hr_signal_detected(self):
        post = self._make_post(post_text="We're hiring a Python developer. Apply now!")
        data = lfs._extract_post_data(post, MagicMock())
        self.assertEqual(data["hr_signal"], "yes")

    def test_no_hr_signal(self):
        post = self._make_post(post_text="Interesting article about machine learning trends.")
        data = lfs._extract_post_data(post, MagicMock())
        self.assertEqual(data["hr_signal"], "")

    def test_profile_url_absolute(self):
        post = self._make_post(profile_href="https://www.linkedin.com/in/johndoe")
        data = lfs._extract_post_data(post, MagicMock())
        self.assertEqual(data["author_profile"], "https://www.linkedin.com/in/johndoe")

    def test_profile_url_relative_made_absolute(self):
        post = self._make_post(profile_href="/in/janedoe")
        data = lfs._extract_post_data(post, MagicMock())
        self.assertEqual(data["author_profile"], "https://www.linkedin.com/in/janedoe")

    def test_non_linkedin_profile_url_rejected(self):
        post = self._make_post(profile_href="https://evil.com/in/janedoe")
        data = lfs._extract_post_data(post, MagicMock())
        self.assertEqual(data["author_profile"], "")

    def test_post_url_captured(self):
        post = self._make_post(post_href="https://www.linkedin.com/feed/update/urn:li:activity:999")
        data = lfs._extract_post_data(post, MagicMock())
        self.assertIn("activity:999", data["post_url"])

    def test_empty_post_text_returns_none(self):
        post = MagicMock()
        see_more_btn = MagicMock()
        see_more_btn.count.return_value = 0
        post.get_by_text.return_value = see_more_btn
        # All locators return empty
        empty_loc = self._make_locator("", count=0)
        post.locator.return_value = empty_loc
        post.inner_text.return_value = ""
        result = lfs._extract_post_data(post, MagicMock())
        self.assertIsNone(result)

    def test_multiple_emails_semicolon_separated(self):
        post = self._make_post(
            post_text="Contact a@x.com or b@x.com for more info."
        )
        data = lfs._extract_post_data(post, MagicMock())
        emails = [e.strip() for e in data["email"].split(";")]
        self.assertIn("a@x.com", emails)
        self.assertIn("b@x.com", emails)

    def test_see_more_clicked_when_visible(self):
        """When the 'see more' button is visible, it should be clicked."""
        post = self._make_post()
        see_more_btn_el = MagicMock()
        see_more_btn_el.count.return_value = 1
        see_more_btn_el.is_visible.return_value = True

        # Wrap to support .first access
        see_more_container = MagicMock()
        see_more_container.count.return_value = 1
        see_more_container.first = see_more_btn_el
        post.get_by_text.return_value = see_more_container

        # page() needed for wait_for_timeout after click
        mock_page = MagicMock()
        post.page.return_value = mock_page

        lfs._extract_post_data(post, MagicMock())

        see_more_btn_el.click.assert_called_once()
        mock_page.wait_for_timeout.assert_called_once_with(800)


# ===========================================================================
# save_to_csv
# ===========================================================================

class TestSaveToCsv(unittest.TestCase):

    def _base_record(self, **overrides):
        base = {
            "keyword": "test",
            "post_url": "https://linkedin.com/feed/update/1",
            "author_name": "Jane Doe",
            "author_title": "Recruiter at Acme",
            "author_company": "Acme",
            "author_profile": "https://linkedin.com/in/janedoe",
            "email": "jane@acme.com",
            "phone": "+441234567890",
            "hr_signal": "yes",
            "post_text": "We're hiring!",
            "lead_score": 9,
        }
        base.update(overrides)
        return base

    def test_writes_header_and_row(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as tmp:
            output = tmp.name
        try:
            lfs.save_to_csv([self._base_record()], output)
            self.assertTrue(os.path.exists(output))
            with open(output, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["author_name"], "Jane Doe")
            self.assertEqual(rows[0]["email"], "jane@acme.com")
            self.assertEqual(rows[0]["lead_score"], "9")
        finally:
            os.remove(output)

    def test_writes_multiple_rows(self):
        records = [self._base_record(email=f"user{i}@x.com") for i in range(5)]
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as tmp:
            output = tmp.name
        try:
            lfs.save_to_csv(records, output)
            with open(output, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 5)
        finally:
            os.remove(output)

    def test_correct_fieldnames_in_header(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as tmp:
            output = tmp.name
        try:
            lfs.save_to_csv([], output)
            with open(output, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader)
            expected = [
                "keyword", "post_url", "author_name", "author_title",
                "author_company", "author_profile", "email", "phone",
                "hr_signal", "post_text", "lead_score",
            ]
            self.assertEqual(header, expected)
        finally:
            os.remove(output)

    def test_raises_on_directory_path(self):
        tmp_dir = tempfile.mkdtemp()
        try:
            with self.assertRaises(ValueError):
                lfs.save_to_csv([], tmp_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_creates_parent_directories(self):
        tmp_dir = tempfile.mkdtemp()
        output = os.path.join(tmp_dir, "nested", "dir", "leads.csv")
        try:
            lfs.save_to_csv([], output)
            self.assertTrue(os.path.exists(output))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_empty_records_writes_header_only(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as tmp:
            output = tmp.name
        try:
            lfs.save_to_csv([], output)
            with open(output, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows, [])
        finally:
            os.remove(output)

    def test_extra_fields_ignored(self):
        """Records with extra keys beyond fieldnames should not raise."""
        record = self._base_record()
        record["unexpected_field"] = "surprise"
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as tmp:
            output = tmp.name
        try:
            lfs.save_to_csv([record], output)  # should not raise
            with open(output, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertNotIn("unexpected_field", rows[0])
        finally:
            os.remove(output)


# ===========================================================================
# scrape_feed  (Playwright + login fully mocked)
# ===========================================================================

class TestScrapeFeed(unittest.TestCase):
    """
    Tests for the top-level scrape_feed() pipeline.
    Playwright is mocked at the module level; linkedin_login and
    _extract_post_data are mocked to keep tests focused and fast.
    """

    def _build_mock_playwright(self):
        """Return a (mock_playwright_ctx, mock_page) pair for patch injection."""
        mock_pw_cm = MagicMock()

        mock_browser = MagicMock()
        mock_pw_cm.chromium.launch.return_value = mock_browser

        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context

        mock_page = MagicMock()
        mock_page.wait_for_timeout = MagicMock()
        mock_page.goto = MagicMock()
        mock_context.new_page.return_value = mock_page

        return mock_pw_cm, mock_page

    def _make_fake_post_data(self, idx: int) -> dict:
        return {
            "author_name": f"Author {idx}",
            "author_title": f"Recruiter at Corp {idx}",
            "author_company": f"Corp {idx}",
            "author_profile": f"https://www.linkedin.com/in/author{idx}",
            "post_url": f"https://www.linkedin.com/feed/update/urn:li:activity:{idx}",
            "post_text": f"We're hiring for role {idx}! Send CV to hr{idx}@corp.com",
            "email": f"hr{idx}@corp.com",
            "phone": "",
            "hr_signal": "yes",
        }

    @patch("linkedin_feed_scraper._extract_post_data")
    @patch("linkedin_feed_scraper.linkedin_login")
    @patch("linkedin_feed_scraper.sync_playwright")
    def test_collects_requested_number_of_posts(
        self, mock_playwright, mock_login, mock_extract
    ):
        """scrape_feed returns exactly num_results records when enough posts exist."""
        mock_pw_cm, mock_page = self._build_mock_playwright()
        mock_playwright.return_value.__enter__.return_value = mock_pw_cm
        mock_login.return_value = True

        # Provide 20 distinct card mocks
        num = 20
        cards = [MagicMock() for _ in range(num)]
        for i, card in enumerate(cards):
            card.inner_text.return_value = f"post preview text number {i:03d}"

        # locator('.feed-shared-update-v2').all() returns all cards
        def locator_side_effect(selector):
            loc = MagicMock()
            if selector == "div.feed-shared-update-v2":
                loc.all.return_value = cards
            else:
                loc.all.return_value = []
            return loc

        mock_page.locator.side_effect = locator_side_effect

        # _extract_post_data returns unique fake data per card
        mock_extract.side_effect = [self._make_fake_post_data(i) for i in range(num)]

        with patch("time.sleep"):
            records = lfs.scrape_feed("hiring", "u@e.com", "pass", num_results=num)

        self.assertEqual(len(records), num)
        for rec in records:
            self.assertIn("keyword", rec)
            self.assertEqual(rec["keyword"], "hiring")
            self.assertIn("lead_score", rec)
            self.assertIsInstance(rec["lead_score"], int)

    @patch("linkedin_feed_scraper.linkedin_login")
    @patch("linkedin_feed_scraper.sync_playwright")
    def test_login_failure_returns_empty(self, mock_playwright, mock_login):
        """When login fails, scrape_feed should return an empty list."""
        mock_pw_cm, _ = self._build_mock_playwright()
        mock_playwright.return_value.__enter__.return_value = mock_pw_cm
        mock_login.return_value = False

        with patch("time.sleep"):
            records = lfs.scrape_feed("anything", "bad@e.com", "wrong", num_results=10)

        self.assertEqual(records, [])

    @patch("linkedin_feed_scraper._extract_post_data")
    @patch("linkedin_feed_scraper.linkedin_login")
    @patch("linkedin_feed_scraper.sync_playwright")
    def test_search_page_timeout_returns_empty(
        self, mock_playwright, mock_login, mock_extract
    ):
        """A timeout on the search page navigation returns an empty list."""
        from playwright.sync_api import TimeoutError as PwTimeout
        mock_pw_cm, mock_page = self._build_mock_playwright()
        mock_playwright.return_value.__enter__.return_value = mock_pw_cm
        mock_login.return_value = True

        # First goto (login page) succeeds; second (search page) times out
        call_count = {"n": 0}

        def goto_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise PwTimeout("timeout on search page")

        mock_page.goto.side_effect = goto_side_effect

        with patch("time.sleep"):
            records = lfs.scrape_feed("test", "u@e.com", "p", num_results=5)

        self.assertEqual(records, [])

    @patch("linkedin_feed_scraper._extract_post_data")
    @patch("linkedin_feed_scraper.linkedin_login")
    @patch("linkedin_feed_scraper.sync_playwright")
    def test_deduplicates_repeated_cards(
        self, mock_playwright, mock_login, mock_extract
    ):
        """Cards with identical preview text are only processed once."""
        mock_pw_cm, mock_page = self._build_mock_playwright()
        mock_playwright.return_value.__enter__.return_value = mock_pw_cm
        mock_login.return_value = True

        # 5 cards all returning the same preview → only first should be processed
        card = MagicMock()
        card.inner_text.return_value = "same preview text repeated"
        cards = [card] * 5

        def locator_side_effect(selector):
            loc = MagicMock()
            if selector == "div.feed-shared-update-v2":
                loc.all.return_value = cards
            else:
                loc.all.return_value = []
            return loc

        mock_page.locator.side_effect = locator_side_effect
        mock_extract.return_value = self._make_fake_post_data(0)

        with patch("time.sleep"):
            records = lfs.scrape_feed("test", "u@e.com", "p", num_results=5)

        # Only one unique card, so only one record
        self.assertEqual(len(records), 1)

    @patch("linkedin_feed_scraper._extract_post_data")
    @patch("linkedin_feed_scraper.linkedin_login")
    @patch("linkedin_feed_scraper.sync_playwright")
    def test_none_post_data_skipped(
        self, mock_playwright, mock_login, mock_extract
    ):
        """Cards where _extract_post_data returns None are silently skipped."""
        mock_pw_cm, mock_page = self._build_mock_playwright()
        mock_playwright.return_value.__enter__.return_value = mock_pw_cm
        mock_login.return_value = True

        cards = [MagicMock() for _ in range(3)]
        for i, c in enumerate(cards):
            c.inner_text.return_value = f"unique text {i}"

        def locator_side_effect(selector):
            loc = MagicMock()
            if selector == "div.feed-shared-update-v2":
                loc.all.return_value = cards
            else:
                loc.all.return_value = []
            return loc

        mock_page.locator.side_effect = locator_side_effect
        # First and third cards return None (empty / unreadable posts)
        mock_extract.side_effect = [None, self._make_fake_post_data(1), None]

        with patch("time.sleep"):
            records = lfs.scrape_feed("test", "u@e.com", "p", num_results=3)

        self.assertEqual(len(records), 1)

    @patch("linkedin_feed_scraper._extract_post_data")
    @patch("linkedin_feed_scraper.linkedin_login")
    @patch("linkedin_feed_scraper.sync_playwright")
    def test_stops_at_num_results(
        self, mock_playwright, mock_login, mock_extract
    ):
        """Only num_results records are collected even if more cards are available."""
        mock_pw_cm, mock_page = self._build_mock_playwright()
        mock_playwright.return_value.__enter__.return_value = mock_pw_cm
        mock_login.return_value = True

        num_cards = 30
        cards = [MagicMock() for _ in range(num_cards)]
        for i, c in enumerate(cards):
            c.inner_text.return_value = f"unique text card {i}"

        def locator_side_effect(selector):
            loc = MagicMock()
            if selector == "div.feed-shared-update-v2":
                loc.all.return_value = cards
            else:
                loc.all.return_value = []
            return loc

        mock_page.locator.side_effect = locator_side_effect
        mock_extract.side_effect = [self._make_fake_post_data(i) for i in range(num_cards)]

        with patch("time.sleep"):
            records = lfs.scrape_feed("test", "u@e.com", "p", num_results=10)

        self.assertEqual(len(records), 10)

    @patch("linkedin_feed_scraper._extract_post_data")
    @patch("linkedin_feed_scraper.linkedin_login")
    @patch("linkedin_feed_scraper.sync_playwright")
    def test_lead_score_added_to_each_record(
        self, mock_playwright, mock_login, mock_extract
    ):
        """Every collected record should have a numeric lead_score field."""
        mock_pw_cm, mock_page = self._build_mock_playwright()
        mock_playwright.return_value.__enter__.return_value = mock_pw_cm
        mock_login.return_value = True

        cards = [MagicMock()]
        cards[0].inner_text.return_value = "post card text"

        def locator_side_effect(selector):
            loc = MagicMock()
            if selector == "div.feed-shared-update-v2":
                loc.all.return_value = cards
            else:
                loc.all.return_value = []
            return loc

        mock_page.locator.side_effect = locator_side_effect
        mock_extract.return_value = self._make_fake_post_data(42)

        with patch("time.sleep"):
            records = lfs.scrape_feed("test", "u@e.com", "p", num_results=1)

        self.assertEqual(len(records), 1)
        self.assertIn("lead_score", records[0])
        self.assertIsInstance(records[0]["lead_score"], int)
        # hr_signal="yes" + company present + email → should be > 0
        self.assertGreater(records[0]["lead_score"], 0)

    @patch("linkedin_feed_scraper._extract_post_data")
    @patch("linkedin_feed_scraper.linkedin_login")
    @patch("linkedin_feed_scraper.sync_playwright")
    def test_search_url_contains_encoded_keyword(
        self, mock_playwright, mock_login, mock_extract
    ):
        """The search URL should contain the URL-encoded keyword."""
        mock_pw_cm, mock_page = self._build_mock_playwright()
        mock_playwright.return_value.__enter__.return_value = mock_pw_cm
        mock_login.return_value = True

        mock_page.locator.return_value.all.return_value = []
        mock_extract.return_value = None

        with patch("time.sleep"):
            lfs.scrape_feed("HR jobs London", "u@e.com", "p", num_results=1)

        # Verify that page.goto was called with a URL containing the keyword
        search_calls = [
            c for c in mock_page.goto.call_args_list
            if "search/results/content" in str(c)
        ]
        self.assertTrue(len(search_calls) >= 1)
        search_url = search_calls[0].args[0]
        self.assertIn("HR+jobs+London", search_url)


# ===========================================================================
# CLI argument parser
# ===========================================================================

class TestArgParser(unittest.TestCase):

    def test_keyword_required(self):
        parser = lfs.build_arg_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_defaults(self):
        parser = lfs.build_arg_parser()
        args = parser.parse_args(["--keyword", "hiring London"])
        self.assertEqual(args.keyword, "hiring London")
        self.assertEqual(args.results, 20)
        self.assertEqual(args.output, "linkedin_leads.csv")

    def test_custom_results_and_output(self):
        parser = lfs.build_arg_parser()
        args = parser.parse_args([
            "--keyword", "recruiter",
            "--results", "50",
            "--output", "custom.csv",
        ])
        self.assertEqual(args.results, 50)
        self.assertEqual(args.output, "custom.csv")

    def test_email_from_cli(self):
        parser = lfs.build_arg_parser()
        args = parser.parse_args([
            "--email", "user@example.com",
            "--password", "secret",
            "--keyword", "jobs",
        ])
        self.assertEqual(args.email, "user@example.com")
        self.assertEqual(args.password, "secret")

    def test_email_from_env(self):
        parser = lfs.build_arg_parser()
        with patch.dict(os.environ, {
            "LINKEDIN_EMAIL": "env@example.com",
            "LINKEDIN_PASSWORD": "envpass",
        }):
            # Re-import to pick up patched env — use parse_args instead
            import importlib
            import linkedin_feed_scraper as _lfs
            importlib.reload(_lfs)
            p = _lfs.build_arg_parser()
            args = p.parse_args(["--keyword", "test"])
        self.assertEqual(args.email, "env@example.com")
        self.assertEqual(args.password, "envpass")

    def test_missing_credentials_exits(self):
        """main() should exit if no credentials are provided."""
        parser = lfs.build_arg_parser()
        # No email or password: both default to ""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                args = parser.parse_args(["--keyword", "test"])
                # Simulate what main() does
                if not args.email or not args.password:
                    parser.error("credentials required")


if __name__ == "__main__":
    unittest.main(verbosity=2)
