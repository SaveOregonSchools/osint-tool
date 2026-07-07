import unittest
import tempfile
from pathlib import Path

import app
import osint_common


class AppSmokeTests(unittest.TestCase):
    def setUp(self):
        app.REGISTRY = {}
        app.PLUGIN_FINGERPRINT = None

    def test_load_plugins_finds_expected_queries(self):
        plugins = app.load_plugins()

        self.assertIn("bluesky_profile_lookup", plugins)
        self.assertIn("bluesky_author_feed_scan", plugins)
        self.assertIn("bluesky_keyword_search", plugins)
        self.assertIn("google_political_ads_search", plugins)
        self.assertIn("meta_ad_library_search", plugins)
        self.assertIn("meta_ad_library_enhanced", plugins)
        self.assertIn("meta_facebook_page_content_search", plugins)
        self.assertIn("osint_wayback_lookup", plugins)
        self.assertIn("tiktok_research_video_search", plugins)
        self.assertIn("youtube_channel_scan", plugins)
        self.assertIn("x_recent_search", plugins)
        self.assertIn("x_full_archive_search", plugins)

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
        self.assertIn("Resources", body)
        self.assertIn("save-oregon-schools-logo.png", body)
        self.assertIn('href="https://github.com/SaveOregonSchools/osint-tool"', body)
        self.assertIn('href="https://github.com/SaveOregonSchools/osint-tool/blob/main/LICENSE"', body)
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
        self.assertIn("Data access mode", body)

    def test_resources_page_renders_evidence_checklist(self):
        app.app.config.update(TESTING=True)

        with app.app.test_client() as client:
            response = client.get("/resources")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Evidence Checklist", body)
        self.assertIn("Bellingcat Online Investigation Toolkit", body)

    def test_health_endpoint_reports_loaded_plugins(self):
        app.app.config.update(TESTING=True)

        with app.app.test_client() as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["ok"], True)
        self.assertIn("bluesky_profile_lookup", payload["plugins"])

    def test_osint_cache_schema_and_core_persistence(self):
        original_db_path = osint_common.OSINT_DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                osint_common.OSINT_DB_PATH = str(Path(tmp) / "cache.db")
                conn = osint_common.connect_cache()
                run_id = osint_common.start_query_run(conn, "pytest", "test_plugin", {"access_token": "secret"})
                row = osint_common.core_row(
                    source_platform="Test",
                    source_api="unit",
                    source_type="manual_entry",
                    target_input="target",
                    text="vote for example",
                    raw_json={"id": "1", "text": "vote for example"},
                    platform_item_id="1",
                )
                item_id = osint_common.persist_core_item(conn, run_id, row)
                osint_common.finish_query_run(conn, run_id, status="ok", result_count=1)
                self.assertIsNotNone(item_id)
                saved_run = conn.execute("SELECT * FROM query_runs WHERE id = ?", (run_id,)).fetchone()
                self.assertEqual(saved_run["status"], "ok")
                self.assertIn("REDACTED", saved_run["params_json"])
                conn.close()
        finally:
            osint_common.OSINT_DB_PATH = original_db_path

    def test_access_mode_blocks_controlled_sources(self):
        with self.assertRaises(RuntimeError):
            osint_common.enforce_source_access({"source_type": "approved_research_api"}, {"data_access_mode": "official"})


if __name__ == "__main__":
    unittest.main()
