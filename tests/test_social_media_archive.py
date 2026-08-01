import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from providers.browsertrix_archive import (
    ArchiveBatch,
    CrawlSettings,
    build_archive_plan,
    build_docker_command,
    build_x_search_query,
    browsertrix_exit_status,
    inclusive_date_periods,
    normalize_x_handle,
)
from queries import social_media_archive


class SocialMediaArchiveTests(unittest.TestCase):
    def test_year_periods_cover_inclusive_range_without_overlap(self):
        periods = inclusive_date_periods(date(2023, 11, 15), date(2025, 2, 3), "year")

        self.assertEqual(
            periods,
            [
                (date(2023, 11, 15), date(2024, 1, 1)),
                (date(2024, 1, 1), date(2025, 1, 1)),
                (date(2025, 1, 1), date(2025, 2, 4)),
            ],
        )

    def test_x_accounts_and_expressions_are_batched_by_year(self):
        plan = build_archive_plan(
            facebook_urls=["https://www.facebook.com/example"],
            instagram_urls=["https://www.instagram.com/example/"],
            x_accounts=["@Example"],
            x_search_expressions=["from:Another has:media"],
            x_additional_terms='"school funding" -filter:replies',
            x_start=date(2023, 1, 1),
            x_end=date(2024, 12, 31),
            batch_mode="year",
        )

        self.assertEqual(len(plan), 6)
        self.assertEqual([item.platform for item in plan[:2]], ["facebook", "instagram"])
        x_batches = plan[2:]
        self.assertEqual({item.period_start for item in x_batches}, {"2023-01-01", "2024-01-01"})
        self.assertEqual({item.period_end for item in x_batches}, {"2023-12-31", "2024-12-31"})
        first_query = parse_qs(urlparse(x_batches[0].seed_url).query)["q"][0]
        self.assertEqual(
            first_query,
            'from:Example "school funding" -filter:replies since:2023-01-01 until:2024-01-01',
        )
        self.assertEqual(len({item.collection for item in plan}), len(plan))

    def test_x_date_operators_are_managed_by_module(self):
        with self.assertRaisesRegex(ValueError, "Do not include since"):
            build_x_search_query("from:example since:2024-01-01", date(2024, 1, 1), date(2025, 1, 1))

    def test_x_handle_accepts_profile_urls(self):
        self.assertEqual(normalize_x_handle("https://x.com/OpenAI"), "OpenAI")
        with self.assertRaisesRegex(ValueError, "Not an X account URL"):
            normalize_x_handle("https://example.com/OpenAI")

    def test_docker_command_uses_isolated_limits_and_read_only_profile(self):
        batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="x",
            label="example — 2024",
            seed_url="https://x.com/search?q=from%3Aexample",
            collection="x-example-2024",
        )
        settings = CrawlSettings(
            image="webrecorder/browsertrix-crawler:1.7.0",
            behavior_timeout_seconds=500,
            time_limit_seconds=1200,
            page_limit=125,
            size_limit_mb=512,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = build_docker_command(
                docker_executable="docker",
                run_dir=root / "run",
                profile_path=root / "profiles" / "social-auth.tar.gz",
                batch=batch,
                settings=settings,
                container_name="osint-test",
            )

        self.assertIn("webrecorder/browsertrix-crawler:1.7.0", command)
        self.assertIn("--alwaysAddBehaviorLinks", command)
        self.assertIn("--failOnContentCheck", command)
        self.assertEqual(command[command.index("--pageLimit") + 1], "125")
        self.assertEqual(command[command.index("--timeLimit") + 1], "1200")
        self.assertEqual(command[command.index("--sizeLimit") + 1], str(512 * 1024 * 1024))
        profile_mount = command[command.index("-v", command.index("-v") + 1) + 1]
        self.assertTrue(profile_mount.endswith(":/profile/profile.tar.gz:ro"))

    def test_browsertrix_limit_exit_codes_keep_completed_archives(self):
        self.assertEqual(browsertrix_exit_status(0), ("completed", ""))
        self.assertEqual(browsertrix_exit_status(14)[0], "completed — size limit reached")
        self.assertEqual(browsertrix_exit_status(15)[0], "completed — time limit reached")
        self.assertEqual(browsertrix_exit_status(9)[0], "failed")

    def test_query_plan_is_non_mutating_and_reports_missing_profile(self):
        form = {
            "operation": "plan",
            "x_accounts": "example",
            "x_start": "2024-01-01",
            "x_end": "2024-12-31",
            "batch_mode": "year",
            "profile_filename": "test-profile.tar.gz",
        }
        with tempfile.TemporaryDirectory() as tmp:
            module_dir = Path(tmp) / "social_media_archive"
            with patch.object(social_media_archive, "MODULE_DATA_DIR", module_dir), patch.object(
                social_media_archive, "PROFILES_DIR", module_dir / "profiles"
            ), patch.object(social_media_archive, "execute_archive_plan") as execute:
                headers, rows = social_media_archive.run(form)

        execute.assert_not_called()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][headers.index("status")], "planned — profile missing")

    def test_empty_plan_has_clear_target_validation_message(self):
        with self.assertRaisesRegex(ValueError, "Specify at least one target profile or URL"):
            social_media_archive.build_plan({})

    def test_successful_plan_results_offer_forced_archive_action(self):
        batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="x",
            label="example — 2024",
            seed_url="https://x.com/search?q=from%3Aexample",
            collection="x-example-2024",
            query_text="from:example since:2024-01-01 until:2025-01-01",
            period_start="2024-01-01",
            period_end="2024-12-31",
        )
        result = social_media_archive._planned_result(batch, profile_exists=True)
        form = {
            "qkey": "social_media_archive",
            "operation": "plan",
            "data_access_mode": "official",
            "x_accounts": "example",
            "x_start": "2024-01-01",
            "x_end": "2024-12-31",
            "batch_mode": "year",
            "profile_filename": "social-auth.tar.gz",
        }

        rendered = social_media_archive.render_results(
            form, social_media_archive.HEADERS, [social_media_archive._result_row(result)]
        )

        self.assertIn(">Run Archiving</button>", rendered)
        self.assertIn('name="operation" value="archive"', rendered)
        self.assertNotIn('name="operation" value="plan"', rendered)
        self.assertIn('name="x_accounts" value="example"', rendered)
        self.assertIn('id="archive-plan-results"', rendered)
        self.assertIn("scrollIntoView", rendered)

    def test_plan_with_missing_profile_does_not_offer_archive_action(self):
        batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="facebook",
            label="example",
            seed_url="https://www.facebook.com/example",
            collection="facebook-example",
        )
        result = social_media_archive._planned_result(batch, profile_exists=False)

        rendered = social_media_archive.render_results(
            {"operation": "plan", "profile_filename": "missing.tar.gz"},
            social_media_archive.HEADERS,
            [social_media_archive._result_row(result)],
        )

        self.assertNotIn(">Run Archiving</button>", rendered)
        self.assertIn("scrollIntoView", rendered)

    def test_archive_operation_queues_each_batch_without_running_inline(self):
        form = {
            "operation": "archive",
            "x_accounts": "example",
            "x_start": "2023-01-01",
            "x_end": "2024-12-31",
            "batch_mode": "year",
            "profile_filename": "test-profile.tar.gz",
            "browsertrix_image": "webrecorder/browsertrix-crawler:1.14.1",
        }
        with tempfile.TemporaryDirectory() as tmp:
            module_dir = Path(tmp) / "social_media_archive"
            profiles_dir = module_dir / "profiles"
            profiles_dir.mkdir(parents=True)
            (profiles_dir / "test-profile.tar.gz").write_bytes(b"profile")
            with patch.object(social_media_archive, "MODULE_DATA_DIR", module_dir), patch.object(
                social_media_archive, "PROFILES_DIR", profiles_dir
            ), patch.object(
                social_media_archive, "enqueue_job", side_effect=["a" * 32, "b" * 32]
            ) as enqueue, patch.object(social_media_archive, "execute_archive_plan") as execute:
                headers, rows = social_media_archive.run(form)

        execute.assert_not_called()
        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(str(row[headers.index("status")]).startswith("queued") for row in rows))


if __name__ == "__main__":
    unittest.main()
