import os
import unittest
import json
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

    def test_toolbox_home_link_is_opt_in(self):
        app.app.config.update(TESTING=True)

        with patch.dict(os.environ, {"TOOLBOX_HOME_URL": ""}):
            with app.app.test_client() as client:
                body = client.get("/").get_data(as_text=True)
        self.assertNotIn('class="button-link toolbox-link"', body)

        with patch.dict(os.environ, {"TOOLBOX_HOME_URL": "/"}):
            with app.app.test_client() as client:
                body = client.get("/").get_data(as_text=True)
        self.assertIn('class="button-link toolbox-link" href="/">All tools</a>', body)

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

    def test_validation_error_is_friendly_without_traceback(self):
        class InvalidQuery:
            META = {
                "key": "social_media_archive",
                "name": "Social Media Archive",
                "description": "Test query",
                "source_type": "manual_entry",
            }

            @staticmethod
            def render_fields(form):
                return '<textarea name="facebook_urls"></textarea>'

            @staticmethod
            def run(form):
                raise ValueError("Specify at least one target profile or URL.")

            @staticmethod
            def export_rows(form):
                return iter(())

        app.app.config.update(TESTING=True)
        app.REGISTRY = {"social_media_archive": InvalidQuery}
        with patch.object(app, "ensure_registry", return_value=None):
            with app.app.test_client() as client:
                response = client.post(
                    "/run",
                    data={"qkey": "social_media_archive", "data_access_mode": "official"},
                )

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Please correct the following", body)
        self.assertIn("Specify at least one target profile or URL", body)
        self.assertNotIn("Traceback", body)
        self.assertIn('id="query-error"', body)

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
            "result": {
                "wacz_path": "runs/example/example.wacz",
                "wacz_bytes": 1234,
                "sha256": "not-for-the-jobs-page",
                "target_url": "https://example.com/source",
            },
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
        self.assertIn("<th>Results</th><th>Source</th>", body)
        self.assertIn(">Download</a>", body)
        self.assertIn(">View source</a>", body)
        self.assertNotIn("SHA-256", body)
        self.assertNotIn("not-for-the-jobs-page", body)
        self.assertNotIn("Open output folder", body)
        self.assertNotIn("Open target", body)
        self.assertNotIn("<th>Details</th>", body)
        self.assertIn('name="refresh"', body)
        self.assertIn('value="15"', body)

    def test_jobs_page_refresh_interval_can_be_disabled_and_is_remembered(self):
        app.app.config.update(TESTING=True)
        counts = {"queued": 1, "running": 0, "completed": 0, "failed": 0}
        with patch.object(app, "ensure_registry"), patch.object(app, "start_worker"), patch.object(
            app, "list_jobs", return_value=[]
        ), patch.object(app, "queue_counts", return_value=counts):
            with app.app.test_client() as client:
                disabled = client.get("/jobs?refresh=0")
                remembered = client.get("/jobs")

        disabled_body = disabled.get_data(as_text=True)
        remembered_body = remembered.get_data(as_text=True)
        self.assertIn("Auto-refresh is off.", disabled_body)
        self.assertNotIn("window.location.reload", disabled_body)
        self.assertIn('value="0"', remembered_body)

    def test_jobs_page_refresh_interval_is_clamped_to_one_hour(self):
        app.app.config.update(TESTING=True)
        counts = {"queued": 1, "running": 0, "completed": 0, "failed": 0}
        with patch.object(app, "ensure_registry"), patch.object(app, "start_worker"), patch.object(
            app, "list_jobs", return_value=[]
        ), patch.object(app, "queue_counts", return_value=counts):
            with app.app.test_client() as client:
                response = client.get("/jobs?refresh=9999")

        body = response.get_data(as_text=True)
        self.assertIn('value="3600"', body)
        self.assertIn("window.location.reload(); }, 3600000", body)

    def test_completed_job_result_download_serves_wacz_from_module_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            wacz_path = data_root / "social_media_archive" / "runs" / "example.wacz"
            wacz_path.parent.mkdir(parents=True)
            wacz_path.write_bytes(b"wacz-result")
            fake_job = {
                "status": "completed",
                "module_key": "social_media_archive",
                "result": {"wacz_path": "runs/example.wacz"},
            }
            app.app.config.update(TESTING=True)
            with patch.object(app, "DATA_ROOT", data_root), patch.object(app, "get_job", return_value=fake_job):
                with app.app.test_client() as client:
                    response = client.get("/jobs/example/download")
                    response_data = response.get_data()
                    content_disposition = response.headers["Content-Disposition"]
                    response.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response_data, b"wacz-result")
        self.assertIn("attachment; filename=example.wacz", content_disposition)

    def test_job_result_download_requires_completed_status(self):
        fake_job = {"status": "running", "module_key": "social_media_archive", "result": {}}
        app.app.config.update(TESTING=True)
        with patch.object(app, "get_job", return_value=fake_job):
            with app.app.test_client() as client:
                response = client.get("/jobs/example/download")

        self.assertEqual(response.status_code, 409)

    def test_job_result_download_rejects_files_outside_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            data_root.mkdir()
            outside = Path(tmp) / "outside.wacz"
            outside.write_bytes(b"private")
            fake_job = {
                "status": "completed",
                "module_key": "social_media_archive",
                "result": {"wacz_path": str(outside)},
            }
            app.app.config.update(TESTING=True)
            with patch.object(app, "DATA_ROOT", data_root), patch.object(app, "get_job", return_value=fake_job):
                with app.app.test_client() as client:
                    response = client.get("/jobs/example/download")

        self.assertEqual(response.status_code, 404)

    def test_health_endpoint_reports_loaded_plugins(self):
        app.app.config.update(TESTING=True)

        with app.app.test_client() as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["ok"], True)
        self.assertIn("bluesky_profile_lookup", payload["plugins"])

    def test_automation_api_submits_and_returns_queue_urls(self):
        class FakeArchive:
            @staticmethod
            def enqueue_profile_review(payload):
                self.assertEqual(payload["lookback"]["unit"], "weeks")
                return {"job_id": "abc123", "status": "queued"}

        app.app.config.update(TESTING=True)
        app.REGISTRY = {"social_media_archive": FakeArchive}
        with patch.dict(app.os.environ, {"OSINT_AUTOMATION_API_TOKEN": "test-token"}), patch.object(
            app, "ensure_registry"
        ):
            with app.app.test_client() as client:
                response = client.post(
                    "/api/v1/social-profile-jobs",
                    headers={"Authorization": "Bearer test-token"},
                    json={"platform": "x", "profile": "example", "lookback": {"value": 3, "unit": "weeks"}},
                )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["status_url"], "/api/v1/social-profile-jobs/abc123")
        self.assertEqual(payload["content_url"], "/api/v1/social-profile-jobs/abc123/content")

    def test_automation_api_requires_configured_bearer_token(self):
        app.app.config.update(TESTING=True)
        with patch.object(app.os, "getenv", return_value=""):
            with app.app.test_client() as client:
                response = client.post("/api/v1/social-profile-jobs", json={})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], "automation_api_not_configured")

    def test_automation_content_endpoint_adds_protected_media_urls(self):
        app.app.config.update(TESTING=True)
        data_dir = Path(app.__file__).resolve().parent / "data"
        data_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=data_dir) as tmp:
            content_dir = Path(tmp) / "content"
            media_dir = content_dir / "media"
            media_dir.mkdir(parents=True)
            (media_dir / "0001-test.png").write_bytes(b"png")
            content_path = content_dir / "content.json"
            content_path.write_text(
                json.dumps({"documents": [{"text": "post"}], "media": [{"file": "media/0001-test.png"}]}),
                encoding="utf-8",
            )
            fake_job = {
                "module_key": "social_media_archive",
                "status": "completed",
                "result": {"content_path": str(content_path)},
            }
            with patch.dict(app.os.environ, {"OSINT_AUTOMATION_API_TOKEN": "test-token"}), patch.object(
                app, "get_job", return_value=fake_job
            ):
                with app.app.test_client() as client:
                    response = client.get(
                        "/api/v1/social-profile-jobs/job1/content",
                        headers={"Authorization": "Bearer test-token"},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["media"][0]["download_url"],
            "/api/v1/social-profile-jobs/job1/media/0001-test.png",
        )

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
