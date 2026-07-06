import unittest

import app


class AppSmokeTests(unittest.TestCase):
    def setUp(self):
        app.REGISTRY = {}

    def test_load_plugins_finds_expected_queries(self):
        plugins = app.load_plugins()

        self.assertIn("bluesky_profile_lookup", plugins)
        self.assertIn("bluesky_author_feed_scan", plugins)
        self.assertIn("bluesky_keyword_search", plugins)
        self.assertIn("meta_ad_library_search", plugins)

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
