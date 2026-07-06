import unittest

import app


class AppSmokeTests(unittest.TestCase):
    def setUp(self):
        app.REGISTRY = {}
        app.PLUGIN_FINGERPRINT = None

    def test_load_plugins_finds_expected_queries(self):
        plugins = app.load_plugins()

        self.assertIn("bluesky_profile_lookup", plugins)
        self.assertIn("bluesky_author_feed_scan", plugins)
        self.assertIn("bluesky_keyword_search", plugins)
        self.assertIn("meta_ad_library_search", plugins)
        self.assertIn("meta_facebook_page_content_search", plugins)
        self.assertIn("x_recent_search", plugins)

    def test_home_lists_modules_without_loading_first_query(self):
        app.app.config.update(TESTING=True)

        with app.app.test_client() as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Select a research module from the list below", body)
        self.assertIn("Platform APIs", body)
        self.assertIn("X Recent Search", body)
        self.assertIn("Facebook Page Posts &amp; Comments", body)
        self.assertIn("save-oregon-schools-logo.png", body)
        self.assertIn('href="https://github.com/SaveOregonSchools"', body)
        self.assertIn("Save Oregon Schools, LLC", body)
        self.assertNotIn("Preview row limit", body)

    def test_query_page_runs_selected_module(self):
        app.app.config.update(TESTING=True)

        with app.app.test_client() as client:
            response = client.get("/query/bluesky_profile_lookup")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('aria-label="Home"', body)
        self.assertIn("Preview row limit", body)

    def test_health_endpoint_reports_loaded_plugins(self):
        app.app.config.update(TESTING=True)

        with app.app.test_client() as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["ok"], True)
        self.assertIn("bluesky_profile_lookup", payload["plugins"])


if __name__ == "__main__":
    unittest.main()
