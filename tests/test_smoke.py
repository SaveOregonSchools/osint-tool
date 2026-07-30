import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

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
        self.assertIn("social_media_archive", plugins)
        self.assertIn("web_page_inspector", plugins)
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
        self.assertIn("Social Media Archive", body)
        self.assertIn("Web Page Technology &amp; Data-Flow Inspector", body)
        self.assertIn("Resources", body)
        self.assertIn("Jobs", body)
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

    def test_run_replaces_post_url_with_query_url_for_safe_refresh(self):
        class FakeQuery:
            META = {
                "key": "web_page_inspector",
                "name": "Web Page Inspector",
                "description": "Test query",
                "source_type": "public_web",
            }

            @staticmethod
            def render_fields(form):
                return '<input name="urls">'

            @staticmethod
            def run(form):
                return ["result"], [["ok"]]

            @staticmethod
            def export_rows(form):
                yield ["ok"]

        app.app.config.update(TESTING=True)
        app.REGISTRY = {"web_page_inspector": FakeQuery}
        with patch.object(app, "ensure_registry", return_value=None):
            with app.app.test_client() as client:
                response = client.post(
                    "/run",
                    data={"qkey": "web_page_inspector", "data_access_mode": "official", "urls": "https://example.org/"},
                )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('window.history.replaceState(null, document.title, "/query/web_page_inspector")', body)

    def test_resources_page_renders_evidence_checklist(self):
        app.app.config.update(TESTING=True)

        with app.app.test_client() as client:
            response = client.get("/resources")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Evidence Checklist", body)
        self.assertIn("Bellingcat Online Investigation Toolkit", body)

    def test_jobs_page_renders_persistent_queue_results(self):
        fake_job = {
            "id": "abc12345deadbeef",
            "group_id": "archive-group",
            "module_key": "social_media_archive",
            "label": "X: example — 2024",
            "status": "completed",
            "progress_current": 1,
            "progress_total": 1,
            "message": "Completed",
            "created_at": "2026-07-30T12:00:00+00:00",
            "error": "",
            "result": {"wacz_path": "runs/example/example.wacz", "output_dir": "C:\\example"},
        }
        app.app.config.update(TESTING=True)
        with patch.object(app, "ensure_registry"), patch.object(app, "start_worker"), patch.object(
            app, "list_jobs", return_value=[fake_job]
        ), patch.object(
            app, "queue_counts", return_value={"queued": 0, "running": 0, "completed": 1, "failed": 0}
        ):
            with app.app.test_client() as client:
                response = client.get("/jobs")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Background Jobs", body)
        self.assertIn("X: example", body)
        self.assertIn("Open output folder", body)

    def test_job_output_folder_action_opens_only_project_data(self):
        output_dir = Path(app.__file__).resolve().parent / "data" / "social_media_archive" / "profiles"
        output_dir.mkdir(parents=True, exist_ok=True)
        fake_job = {"result": {"output_dir": str(output_dir)}}
        app.app.config.update(TESTING=True)
        with patch.object(app, "ensure_registry"), patch.object(app, "get_job", return_value=fake_job), patch.object(
            app.os, "startfile", create=True
        ) as startfile:
            with app.app.test_client() as client:
                response = client.post("/jobs/example/open-folder")

        self.assertEqual(response.status_code, 302)
        startfile.assert_called_once_with(str(output_dir.resolve()))

    def test_job_output_folder_action_rejects_outside_path(self):
        fake_job = {"result": {"output_dir": str(Path(app.__file__).resolve().parent.parent)}}
        app.app.config.update(TESTING=True)
        with patch.object(app, "ensure_registry"), patch.object(app, "get_job", return_value=fake_job), patch.object(
            app.os, "startfile", create=True
        ) as startfile:
            with app.app.test_client() as client:
                response = client.post("/jobs/example/open-folder")

        self.assertEqual(response.status_code, 400)
        startfile.assert_not_called()

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
