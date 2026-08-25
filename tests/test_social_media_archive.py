import gzip
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from providers.browsertrix_archive import (
    ArchiveBatch,
    ArchiveResult,
    CrawlSettings,
    XAuthenticationPreflight,
    build_archive_plan,
    build_docker_command,
    build_interactive_profile_command,
    build_x_auth_preflight_command,
    build_x_search_query,
    browsertrix_exit_status,
    compact_archive_output,
    inclusive_date_periods,
    normalize_x_handle,
)
from providers import browsertrix_archive
from providers.wacz_content import XCaptureInspection, extract_wacz_content, inspect_x_wacz
import app as osint_app
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
        with self.assertRaisesRegex(ValueError, "Invalid X account name"):
            normalize_x_handle("this_handle_is_too_long")

    def test_docker_command_uses_isolated_limits_and_read_only_profile(self):
        batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="x",
            label="example — 2024",
            seed_url="https://x.com/search?q=from%3Aexample",
            collection="x-example-2024",
        )
        settings = CrawlSettings(
            image="webrecorder/browsertrix-crawler:1.14.3",
            behavior_timeout_seconds=500,
            time_limit_seconds=1200,
            page_limit=125,
            size_limit_mb=512,
            x_rate_limit_max_retries=4,
            x_rate_limit_interrupt_count=-1,
            x_post_load_delay_seconds=10,
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

        self.assertIn("webrecorder/browsertrix-crawler:1.14.3", command)
        self.assertIn("--alwaysAddBehaviorLinks", command)
        self.assertIn("--failOnContentCheck", command)
        self.assertIn("--rateLimitOnMatch", command)
        self.assertEqual(command.count("--rateLimitOnMatch"), 2)
        self.assertEqual(command[command.index("--rateLimitInterruptCount") + 1], "-1")
        self.assertEqual(command[command.index("--rateLimitMaxRetries") + 1], "4")
        self.assertEqual(command[command.index("--rateLimitTimeout") + 1], "900")
        self.assertEqual(command[command.index("--postLoadDelay") + 1], "10")
        self.assertEqual(command[command.index("--pageLimit") + 1], "125")
        self.assertEqual(command[command.index("--timeLimit") + 1], "1200")
        self.assertEqual(command[command.index("--sizeLimit") + 1], str(512 * 1024 * 1024))
        profile_mount = command[command.index("-v", command.index("-v") + 1) + 1]
        self.assertTrue(profile_mount.endswith(":/profile/profile.tar.gz:ro"))

    def test_x_crawl_tuning_defaults_are_loaded_from_environment(self):
        environment = {
            "OSINT_X_RATE_LIMIT_MAX_RETRIES": "7",
            "OSINT_X_RATE_LIMIT_INTERRUPT_COUNT": "3",
            "OSINT_X_POST_LOAD_DELAY_SECONDS": "15",
        }
        with patch.dict(browsertrix_archive.os.environ, environment):
            settings = CrawlSettings()

        self.assertEqual(settings.x_rate_limit_max_retries, 7)
        self.assertEqual(settings.x_rate_limit_interrupt_count, 3)
        self.assertEqual(settings.x_post_load_delay_seconds, 15)

    def test_invalid_x_crawl_tuning_environment_is_rejected(self):
        with patch.dict(
            browsertrix_archive.os.environ,
            {"OSINT_X_POST_LOAD_DELAY_SECONDS": "601"},
        ):
            with self.assertRaisesRegex(ValueError, "OSINT_X_POST_LOAD_DELAY_SECONDS"):
                CrawlSettings()

    def test_x_rate_limit_flags_reject_explicitly_old_images_but_allow_digest_pins(self):
        x_batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="x",
            label="example",
            seed_url="https://x.com/search?q=from%3Aexample",
            collection="x-example",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "1.14.0 or newer"):
                build_docker_command(
                    docker_executable="docker",
                    run_dir=root / "run",
                    profile_path=root / "profile.tar.gz",
                    batch=x_batch,
                    settings=CrawlSettings(image="webrecorder/browsertrix-crawler:1.13.3"),
                    container_name="old-image",
                )
            command = build_docker_command(
                docker_executable="docker",
                run_dir=root / "run",
                profile_path=root / "profile.tar.gz",
                batch=x_batch,
                settings=CrawlSettings(
                    image="webrecorder/browsertrix-crawler:1.14.1@sha256:" + "a" * 64
                ),
                container_name="digest-image",
            )

        self.assertIn("--rateLimitOnMatch", command)

    def test_non_x_command_does_not_add_x_rate_limit_flags(self):
        batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="facebook",
            label="example",
            seed_url="https://www.facebook.com/example",
            collection="facebook-example",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = build_docker_command(
                docker_executable="docker",
                run_dir=root / "run",
                profile_path=root / "profile.tar.gz",
                batch=batch,
                settings=CrawlSettings(image="webrecorder/browsertrix-crawler:1.7.0"),
                container_name="facebook-test",
            )

        self.assertNotIn("--rateLimitOnMatch", command)
        self.assertNotIn("--postLoadDelay", command)

    def test_x_auth_preflight_command_is_one_page_temporary_and_profile_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = build_x_auth_preflight_command(
                docker_executable="docker",
                work_dir=root / "preflight",
                profile_path=root / "profiles" / "social-auth.tar.gz",
                image="webrecorder/browsertrix-crawler:1.14.1",
                behavior_path=root / "x_auth_preflight.js",
                container_name="x-auth-test",
            )

        self.assertIn("https://x.com/settings/account", command)
        # Browsertrix v1.14.1 suppresses --saveProfile output in dry-run mode.
        self.assertNotIn("--dryRun", command)
        self.assertIn("--failOnContentCheck", command)
        self.assertIn("--saveProfile", command)
        self.assertEqual(command[command.index("--pageLimit") + 1], "1")
        self.assertEqual(command[command.index("--scopeType") + 1], "page")
        self.assertEqual(command[command.index("--behaviors") + 1], "siteSpecific")
        self.assertNotIn("--generateWACZ", command)
        self.assertNotIn("autoscroll", command)
        self.assertFalse(any("SearchTimeline" in value for value in command))
        profile_mount = next(value for value in command if value.endswith(":/profile/profile.tar.gz:ro"))
        behavior_mount = next(value for value in command if value.endswith(":/behaviors/x_auth_preflight.js:ro"))
        self.assertTrue(profile_mount)
        self.assertTrue(behavior_mount)

    def test_interactive_profile_reopen_command_is_loopback_only_and_stages_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = build_interactive_profile_command(
                profiles_dir=Path(tmp),
                image="webrecorder/browsertrix-crawler:1.14.1",
                output_filename="social-auth-reauth.tar.gz",
                existing_profile_filename="social-auth.tar.gz",
            )

        self.assertIn("127.0.0.1:6080:6080", command)
        self.assertIn("127.0.0.1:9223:9223", command)
        self.assertNotIn("0.0.0.0", command)
        self.assertIn("create-login-profile", command)
        self.assertIn("social-auth-reauth.tar.gz", command)
        self.assertIn("old-profile.tar.gz", command)
        self.assertIn(":/crawls", command)
        self.assertNotIn(":/crawls/profiles", command)
        self.assertIn("--filename /crawls/social-auth-reauth.tar.gz", command)

    def test_interactive_profile_command_uses_linux_shell_setup_on_posix_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles_dir = Path(tmp) / "profiles"
            with patch.object(browsertrix_archive.os, "name", "posix"):
                command = build_interactive_profile_command(
                    profiles_dir=profiles_dir,
                    image="webrecorder/browsertrix-crawler:1.14.1",
                    output_filename="social-auth.tar.gz",
                )

        self.assertTrue(command.startswith("mkdir -p "))
        self.assertIn("\ndocker run --rm -it", command)
        self.assertNotIn("New-Item", command)
        self.assertIn("127.0.0.1:9223:9223", command)

    def test_x_auth_behavior_emits_only_safe_identity_state(self):
        behavior = browsertrix_archive.X_AUTH_PREFLIGHT_BEHAVIOR.read_text(encoding="utf-8")

        self.assertIn('return "Twitter"', behavior)
        self.assertIn("AppTabBar_Profile_Link", behavior)
        self.assertIn("SideNav_AccountSwitcher_Button", behavior)
        self.assertIn('profileLink.getAttribute("href")', behavior)
        self.assertIn("accountSwitcher.innerText", behavior)
        self.assertIn("x_auth_preflight_verified", behavior)
        self.assertNotIn('input[name="text"]', behavior)
        self.assertNotIn("auth_token", behavior)
        self.assertNotIn("x-csrf-token", behavior)
        self.assertNotIn("authorization", behavior.casefold())

    def test_x_auth_preflight_promotes_only_a_verified_refreshed_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "social-auth.tar.gz"
            profile.write_bytes(b"COOKIE_SECRET_OLD")
            browsertrix_archive._X_AUTH_CACHE.clear()

            def fake_run(command, **kwargs):
                crawl_mount = next(value for value in command if value.endswith(":/crawls"))
                work_dir = Path(crawl_mount.rsplit(":/crawls", 1)[0])
                self._write_valid_browser_profile(work_dir / "refreshed-profile.tar.gz", b"COOKIE_SECRET_NEW")
                return SimpleNamespace(
                    returncode=0,
                    stdout='{"message":"x_auth_preflight_verified handle=@crawler_account"}',
                    stderr="",
                )

            with patch.object(browsertrix_archive.subprocess, "run", side_effect=fake_run):
                result = browsertrix_archive.preflight_x_authentication(
                    executable="docker",
                    profile_path=profile,
                    image="webrecorder/browsertrix-crawler:1.14.1",
                    expected_handle="crawler_account",
                )

            with tarfile.open(profile, "r:gz") as promoted:
                cookie_member = next(member for member in promoted.getmembers() if Path(member.name).name == "Cookies")
                marker = promoted.extractfile(cookie_member).read()

        self.assertTrue(result.verified)
        self.assertEqual(result.state, "verified")
        self.assertEqual(result.account_handle, "crawler_account")
        self.assertEqual(result.profile_refresh_status, "refreshed")
        self.assertEqual(marker, b"COOKIE_SECRET_NEW")
        serialized = json.dumps(asdict(result))
        self.assertNotIn("COOKIE_SECRET_OLD", serialized)
        self.assertNotIn("COOKIE_SECRET_NEW", serialized)

    def test_x_auth_preflight_caches_logout_and_never_replaces_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "social-auth.tar.gz"
            original = b"original-profile"
            profile.write_bytes(original)
            browsertrix_archive._X_AUTH_CACHE.clear()
            completed = SimpleNamespace(
                returncode=9,
                stdout='{"message":"x_auth_preflight_logged_out"}',
                stderr="",
            )
            with patch.object(browsertrix_archive.subprocess, "run", return_value=completed) as run:
                first = browsertrix_archive.preflight_x_authentication(
                    executable="docker",
                    profile_path=profile,
                    image="webrecorder/browsertrix-crawler:1.14.1",
                )
                second = browsertrix_archive.preflight_x_authentication(
                    executable="docker",
                    profile_path=profile,
                    image="webrecorder/browsertrix-crawler:1.14.1",
                )
            retained = profile.read_bytes()

        self.assertEqual(first.state, "logged_out")
        self.assertTrue(first.reauthentication_required)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(retained, original)
        self.assertIn("create-login-profile", first.reauthentication_command)
        self.assertIn("127.0.0.1:9223:9223", first.reauthentication_command)
        self.assertIn("<user>@<server>", first.ssh_tunnel_command)

    def test_x_auth_preflight_retries_indeterminate_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "social-auth.tar.gz"
            profile.write_bytes(b"profile")
            browsertrix_archive._X_AUTH_CACHE.clear()
            completed = SimpleNamespace(returncode=0, stdout="no authentication signal", stderr="")
            with patch.object(browsertrix_archive.subprocess, "run", return_value=completed) as run:
                result = browsertrix_archive.preflight_x_authentication(
                    executable="docker",
                    profile_path=profile,
                    image="webrecorder/browsertrix-crawler:1.14.1",
                )

        self.assertEqual(result.state, "indeterminate")
        self.assertFalse(result.verified)
        self.assertFalse(result.reauthentication_required)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(run.call_count, 2)

    def test_x_auth_preflight_rejects_wrong_expected_account_before_profile_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "social-auth.tar.gz"
            original = b"original-profile"
            profile.write_bytes(original)
            browsertrix_archive._X_AUTH_CACHE.clear()

            def fake_run(command, **kwargs):
                crawl_mount = next(value for value in command if value.endswith(":/crawls"))
                work_dir = Path(crawl_mount.rsplit(":/crawls", 1)[0])
                self._write_valid_browser_profile(work_dir / "refreshed-profile.tar.gz")
                return SimpleNamespace(
                    returncode=0,
                    stdout='{"message":"x_auth_preflight_verified handle=@other_account"}',
                    stderr="",
                )

            with patch.object(browsertrix_archive.subprocess, "run", side_effect=fake_run):
                result = browsertrix_archive.preflight_x_authentication(
                    executable="docker",
                    profile_path=profile,
                    image="webrecorder/browsertrix-crawler:1.14.1",
                    expected_handle="expected_acct",
                )

            retained = profile.read_bytes()

        self.assertEqual(result.state, "wrong_account")
        self.assertFalse(result.verified)
        self.assertTrue(result.reauthentication_required)
        self.assertEqual(retained, original)
        self.assertIn("@other_account", result.detail)
        self.assertIn("@expected_acct", result.detail)

    def test_x_auth_preflight_fails_closed_when_verified_profile_is_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "social-auth.tar.gz"
            original = b"original-profile"
            profile.write_bytes(original)
            browsertrix_archive._X_AUTH_CACHE.clear()
            completed = SimpleNamespace(
                returncode=0,
                stdout='{"message":"x_auth_preflight_verified handle=@crawler_account"}',
                stderr="",
            )
            with patch.object(browsertrix_archive.subprocess, "run", return_value=completed) as run:
                result = browsertrix_archive.preflight_x_authentication(
                    executable="docker",
                    profile_path=profile,
                    image="webrecorder/browsertrix-crawler:1.14.1",
                )

            retained = profile.read_bytes()

        self.assertEqual(result.state, "indeterminate")
        self.assertFalse(result.verified)
        self.assertEqual(result.profile_refresh_status, "not_saved")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(retained, original)
        self.assertIn("did not emit the required refreshed profile", result.detail)

    def test_x_auth_preflight_does_not_cache_a_positive_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "social-auth.tar.gz"
            self._write_valid_browser_profile(profile, b"COOKIE_OLD")
            browsertrix_archive._X_AUTH_CACHE.clear()

            def fake_run(command, **kwargs):
                crawl_mount = next(value for value in command if value.endswith(":/crawls"))
                work_dir = Path(crawl_mount.rsplit(":/crawls", 1)[0])
                self._write_valid_browser_profile(work_dir / "refreshed-profile.tar.gz", b"COOKIE_NEW")
                return SimpleNamespace(
                    returncode=0,
                    stdout='{"message":"x_auth_preflight_verified handle=@crawler_account"}',
                    stderr="",
                )

            with patch.object(browsertrix_archive.subprocess, "run", side_effect=fake_run) as run:
                first = browsertrix_archive.preflight_x_authentication(
                    executable="docker",
                    profile_path=profile,
                    image="webrecorder/browsertrix-crawler:1.14.1",
                )
                second = browsertrix_archive.preflight_x_authentication(
                    executable="docker",
                    profile_path=profile,
                    image="webrecorder/browsertrix-crawler:1.14.1",
                )

        self.assertTrue(first.verified)
        self.assertTrue(second.verified)
        self.assertFalse(first.cache_hit)
        self.assertFalse(second.cache_hit)
        self.assertEqual(run.call_count, 2)

    def test_x_auth_log_parser_gives_logged_out_state_precedence(self):
        state, handle = browsertrix_archive._preflight_signal(
            'x_auth_preflight_verified handle=@crawler_account\n'
            'x_auth_preflight_logged_out'
        )

        self.assertEqual(state, "logged_out")
        self.assertEqual(handle, "")

    def test_refreshed_profile_validator_rejects_symbolic_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "unsafe-profile.tar.gz"
            with tarfile.open(profile, "w:gz") as archive:
                for name, content in (
                    ("profile/Local State", b"{}"),
                    ("profile/Default/Network/Cookies", b"cookies"),
                ):
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                link = tarfile.TarInfo("profile/unsafe-link")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../outside"
                archive.addfile(link)

            with self.assertRaisesRegex(RuntimeError, "unsafe archive member type"):
                browsertrix_archive._validate_browser_profile_archive(profile)

    def test_browsertrix_limit_exit_codes_keep_completed_archives(self):
        self.assertEqual(browsertrix_exit_status(0), ("completed", ""))
        self.assertEqual(browsertrix_exit_status(14)[0], "completed — size limit reached")
        self.assertEqual(browsertrix_exit_status(15)[0], "completed — time limit reached")
        self.assertEqual(browsertrix_exit_status(18)[0], "rate limited")
        self.assertEqual(browsertrix_exit_status(9)[0], "failed")

    @staticmethod
    def _http_warc_record(record_type, target, status, headers=None, body=b""):
        http_headers = {"Content-Type": "application/json", "Content-Length": str(len(body)), **(headers or {})}
        http_block = (
            f"HTTP/1.1 {status}\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in http_headers.items())
            + "\r\n"
        ).encode("utf-8") + body
        warc_headers = (
            "WARC/1.1\r\n"
            f"WARC-Type: {record_type}\r\n"
            f"WARC-Target-URI: {target}\r\n"
            "Content-Type: application/http; msgtype=response\r\n"
            f"Content-Length: {len(http_block)}\r\n\r\n"
        ).encode("utf-8")
        return warc_headers + http_block + b"\r\n\r\n"

    @staticmethod
    def _http_warc_request(target, headers=None, body=b""):
        http_headers = {"Content-Length": str(len(body)), **(headers or {})}
        http_block = (
            "GET / HTTP/1.1\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in http_headers.items())
            + "\r\n"
        ).encode("utf-8") + body
        warc_headers = (
            "WARC/1.1\r\n"
            "WARC-Type: request\r\n"
            f"WARC-Target-URI: {target}\r\n"
            "Content-Type: application/http; msgtype=request\r\n"
            f"Content-Length: {len(http_block)}\r\n\r\n"
        ).encode("utf-8")
        return warc_headers + http_block + b"\r\n\r\n"

    def _write_x_wacz(self, root, records, page_text="Search results"):
        wacz = Path(root) / "x-test.wacz"
        with zipfile.ZipFile(wacz, "w") as archive:
            archive.writestr(
                "pages/pages.jsonl",
                json.dumps({"format": "json-pages-1.0"})
                + "\n"
                + json.dumps(
                    {
                        "url": "https://x.com/search?q=from%3Aexample&f=live",
                        "title": "Search / X",
                        "text": page_text,
                    }
                )
                + "\n",
            )
            archive.writestr("archive/data.warc.gz", gzip.compress(b"".join(records)))
        return wacz

    @staticmethod
    def _write_valid_wacz(path, *, archive_content=None, archive_hash=None):
        members = {
            "archive/data.warc.gz": archive_content or gzip.compress(b"WARC/1.1\r\n"),
            "pages/pages.jsonl": b'{"format":"json-pages-1.0"}\n',
            "indexes/index.cdx.gz": b"index",
        }
        resources = [
            {
                "name": Path(name).name,
                "path": name,
                "bytes": len(content),
                "hash": (
                    archive_hash
                    if name == "archive/data.warc.gz" and archive_hash is not None
                    else "sha256:" + hashlib.sha256(content).hexdigest()
                ),
            }
            for name, content in members.items()
        ]
        datapackage = json.dumps(
            {"resources": resources, "wacz_version": "1.1.1"},
            separators=(",", ":"),
        ).encode("utf-8")
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
            archive.writestr("datapackage.json", datapackage)
            archive.writestr(
                "datapackage-digest.json",
                json.dumps(
                    {
                        "path": "datapackage.json",
                        "hash": "sha256:" + hashlib.sha256(datapackage).hexdigest(),
                    }
                ),
            )
        return path

    @staticmethod
    def _write_valid_browser_profile(path, marker=b"profile"):
        with tarfile.open(path, "w:gz") as archive:
            for name, content in (
                ("profile/Local State", b"{}"),
                ("profile/Default/Network/Cookies", marker),
            ):
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        return path

    def test_x_wacz_inspector_records_authenticated_state_without_serializing_secrets(self):
        target = "https://x.com/i/api/graphql/hash/SearchTimeline?variables=test"
        request = self._http_warc_request(
            target,
            {
                "Authorization": "Bearer SECRET_AUTHORIZATION_VALUE",
                "Cookie": "auth_token=SECRET_AUTH_TOKEN; ct0=SECRET_CSRF_COOKIE; twid=u%3D1",
                "x-csrf-token": "SECRET_CSRF_HEADER",
                "x-twitter-auth-type": "OAuth2Session",
                "x-twitter-active-user": "yes",
            },
        )
        response = self._http_warc_record(
            "response",
            target,
            "200 OK",
            {"x-rate-limit-remaining": "20"},
            b'{"data":{"search_by_raw_query":{"search_timeline":{"timeline":{"instructions":[]}}}}}',
        )
        with tempfile.TemporaryDirectory() as tmp:
            wacz = self._write_x_wacz(
                tmp,
                [request, response],
                page_text="Home Explore Notifications Messages Bookmarks Communities Premium Profile More Post",
            )
            result = inspect_x_wacz(wacz)

        self.assertEqual(result.authentication_state, "authenticated")
        self.assertEqual(result.authenticated_requests, 1)
        self.assertEqual(result.guest_requests, 0)
        self.assertTrue(result.logged_in_ui)
        serialized = json.dumps(asdict(result))
        self.assertNotIn("SECRET_AUTH_TOKEN", serialized)
        self.assertNotIn("SECRET_CSRF", serialized)
        self.assertNotIn("SECRET_AUTHORIZATION", serialized)

    def test_x_wacz_inspector_marks_explicit_login_ui_as_authentication_failure(self):
        request = self._http_warc_request(
            "https://x.com/i/api/graphql/hash/Viewer?variables=test",
            {"x-guest-token": "SECRET_GUEST_TOKEN"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            wacz = self._write_x_wacz(
                tmp,
                [request],
                page_text="Happening now Join today Create account Sign up Log in",
            )
            result = inspect_x_wacz(wacz)

        self.assertEqual(result.authentication_state, "logged_out")
        self.assertEqual(result.classification, "authentication_failed")
        self.assertEqual(result.authenticated_requests, 0)
        self.assertEqual(result.guest_requests, 1)
        self.assertNotIn("SECRET_GUEST_TOKEN", json.dumps(asdict(result)))

    def test_x_wacz_inspector_accepts_a_valid_empty_search(self):
        target = "https://x.com/i/api/graphql/hash/SearchTimeline?variables=test"
        record = self._http_warc_record(
            "response",
            target,
            "200 OK",
            {"x-rate-limit-limit": "50", "x-rate-limit-remaining": "26", "x-rate-limit-reset": "1786567895"},
            b'{"data":{"search_by_raw_query":{"search_timeline":{"timeline":{"instructions":[]}}}}}',
        )
        with tempfile.TemporaryDirectory() as tmp:
            inspection = inspect_x_wacz(self._write_x_wacz(tmp, [record]), now_epoch=1786567000)

        self.assertEqual(inspection.classification, "valid")
        self.assertEqual(inspection.search_successes, 1)
        self.assertEqual(inspection.rate_limit_remaining, 26)

    def test_x_wacz_inspector_pairs_remaining_with_the_latest_rate_window(self):
        target = "https://x.com/i/api/graphql/hash/SearchTimeline?variables=test"
        body = b'{"data":{"search_by_raw_query":{"search_timeline":{"timeline":{"instructions":[]}}}}}'
        old_window = self._http_warc_record(
            "response",
            target,
            "200 OK",
            {"x-rate-limit-remaining": "0", "x-rate-limit-reset": "1786567000"},
            body,
        )
        new_window = self._http_warc_record(
            "response",
            target,
            "200 OK",
            {"x-rate-limit-remaining": "49", "x-rate-limit-reset": "1786568000"},
            body,
        )
        with tempfile.TemporaryDirectory() as tmp:
            inspection = inspect_x_wacz(self._write_x_wacz(tmp, [old_window, new_window]))

        self.assertEqual(inspection.rate_limit_reset, 1786568000)
        self.assertEqual(inspection.rate_limit_remaining, 49)

    def test_x_wacz_inspector_detects_revisit_429_before_any_results(self):
        target = "https://x.com/i/api/graphql/hash/SearchTimeline?variables=test"
        limited = self._http_warc_record(
            "revisit",
            target,
            "429 Too Many Requests",
            {"x-rate-limit-limit": "50", "x-rate-limit-remaining": "0", "x-rate-limit-reset": "1786567895"},
            b"Rate limit exceeded",
        )
        with tempfile.TemporaryDirectory() as tmp:
            inspection = inspect_x_wacz(
                self._write_x_wacz(tmp, [limited], "Something went wrong. Try reloading. Retry"),
                now_epoch=1786567000,
            )

        self.assertEqual(inspection.classification, "rate_limited_empty")
        self.assertTrue(inspection.is_retryable)
        self.assertEqual(inspection.search_rate_limits, 1)
        self.assertEqual(inspection.rate_limit_reset, 1786567895)

    def test_x_wacz_inspector_marks_success_then_429_as_partial(self):
        target = "https://x.com/i/api/graphql/hash/SearchTimeline?variables=test"
        success = self._http_warc_record(
            "response",
            target,
            "200 OK",
            {"x-rate-limit-remaining": "1", "x-rate-limit-reset": "1786567895"},
            b'{"data":{"search_by_raw_query":{"search_timeline":{"timeline":{"instructions":[]}}}}}',
        )
        limited = self._http_warc_record(
            "revisit",
            target,
            "429 Too Many Requests",
            {"x-rate-limit-remaining": "0", "x-rate-limit-reset": "1786567895"},
            b"Rate limit exceeded",
        )
        with tempfile.TemporaryDirectory() as tmp:
            inspection = inspect_x_wacz(self._write_x_wacz(tmp, [success, limited]), now_epoch=1786567000)

        self.assertEqual(inspection.classification, "rate_limited_partial")
        self.assertTrue(inspection.is_partial)
        self.assertEqual(inspection.search_successes, 1)
        self.assertEqual(inspection.search_rate_limits, 1)

    def test_x_wacz_inspector_detects_rate_limit_in_http_200_body(self):
        target = "https://x.com/i/api/graphql/hash/SearchTimeline?variables=test"
        limited = self._http_warc_record(
            "response",
            target,
            "200 OK",
            {"x-rate-limit-remaining": "0", "x-rate-limit-reset": "1786567895"},
            b'{"errors":[{"code":88,"message":"Rate limit exceeded"}]}',
        )
        with tempfile.TemporaryDirectory() as tmp:
            inspection = inspect_x_wacz(self._write_x_wacz(tmp, [limited]), now_epoch=1786567000)

        self.assertEqual(inspection.classification, "rate_limited_empty")
        self.assertEqual(inspection.search_rate_limits, 1)

    def test_x_wacz_inspector_does_not_count_an_empty_revisit_as_success(self):
        target = "https://x.com/i/api/graphql/hash/SearchTimeline?variables=test"
        limited = self._http_warc_record(
            "response", target, "200 OK", {}, b'{"errors":[{"code":88,"message":"Rate limit exceeded"}]}'
        )
        revisit = self._http_warc_record("revisit", target, "200 OK")
        with tempfile.TemporaryDirectory() as tmp:
            inspection = inspect_x_wacz(self._write_x_wacz(tmp, [limited, revisit]))

        self.assertEqual(inspection.classification, "rate_limited_empty")
        self.assertEqual(inspection.search_successes, 0)

    def test_x_wacz_inspector_marks_non_rate_failure_after_success_as_partial(self):
        target = "https://x.com/i/api/graphql/hash/SearchTimeline?variables=test"
        success = self._http_warc_record(
            "response",
            target,
            "200 OK",
            {},
            b'{"data":{"search_by_raw_query":{"search_timeline":{"timeline":{"instructions":[]}}}}}',
        )
        failed = self._http_warc_record("response", target, "500 Server Error", {}, b"failure")
        with tempfile.TemporaryDirectory() as tmp:
            inspection = inspect_x_wacz(self._write_x_wacz(tmp, [success, failed]))

        self.assertEqual(inspection.classification, "invalid_partial")
        self.assertTrue(inspection.is_partial)
        self.assertEqual(inspection.search_other_statuses, 1)

    def test_x_wacz_inspector_treats_search_503_as_retryable_rate_limit(self):
        target = "https://x.com/i/api/graphql/hash/SearchTimeline?variables=test"
        limited = self._http_warc_record("response", target, "503 Service Unavailable", {}, b"try later")
        with tempfile.TemporaryDirectory() as tmp:
            inspection = inspect_x_wacz(self._write_x_wacz(tmp, [limited]))

        self.assertEqual(inspection.classification, "rate_limited_empty")
        self.assertTrue(inspection.is_retryable)

    def test_x_wacz_inspector_ignores_unrelated_429(self):
        limited = self._http_warc_record(
            "response",
            "https://x.com/i/api/graphql/hash/SidebarUserRecommendations?variables=test",
            "429 Too Many Requests",
            {"x-rate-limit-remaining": "0"},
            b"Rate limit exceeded",
        )
        with tempfile.TemporaryDirectory() as tmp:
            inspection = inspect_x_wacz(self._write_x_wacz(tmp, [limited]))

        self.assertEqual(inspection.classification, "invalid")
        self.assertEqual(inspection.search_rate_limits, 0)

    def test_compact_archive_output_keeps_only_verified_wacz_and_jsons(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            collection_dir = run_dir / "collections" / "x-example"
            (collection_dir / "downloads").mkdir(parents=True)
            (collection_dir / "archive").mkdir()
            (collection_dir / "downloads" / "profile.tar.gz").write_bytes(b"profile")
            (collection_dir / "archive" / "raw.warc.gz").write_bytes(b"warc")
            wacz = collection_dir / "x-example.wacz"
            self._write_valid_wacz(wacz)
            log = run_dir / "batch.log"
            log.write_text("crawler output", encoding="utf-8")
            (run_dir / "plan.json").write_text("{}", encoding="utf-8")
            (run_dir / "manifest.json").write_text("{}", encoding="utf-8")

            pruned_files, pruned_bytes = compact_archive_output(run_dir, "x-example", wacz, log)

            self.assertEqual(pruned_files, 3)
            self.assertGreater(pruned_bytes, 0)
            self.assertEqual([item.name for item in collection_dir.iterdir()], ["x-example.wacz"])
            self.assertTrue((run_dir / "plan.json").is_file())
            self.assertTrue((run_dir / "manifest.json").is_file())
            self.assertFalse(log.exists())

    def test_compaction_refusal_is_non_mutating_for_an_external_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            collection_dir = run_dir / "collections" / "x-example"
            collection_dir.mkdir(parents=True)
            raw = collection_dir / "raw.warc.gz"
            raw.write_bytes(b"raw")
            wacz = self._write_valid_wacz(collection_dir / "x-example.wacz")
            outside_log = root / "outside.log"
            outside_log.write_text("log", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "outside its run directory"):
                compact_archive_output(run_dir, "x-example", wacz, outside_log)

            self.assertTrue(raw.is_file())
            self.assertTrue(wacz.is_file())
            self.assertTrue(outside_log.is_file())

    def test_compaction_refuses_an_incomplete_wacz_without_deleting_work_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            collection_dir = run_dir / "collections" / "x-example"
            collection_dir.mkdir(parents=True)
            raw = collection_dir / "raw.warc.gz"
            raw.write_bytes(b"raw")
            wacz = collection_dir / "x-example.wacz"
            with zipfile.ZipFile(wacz, "w") as archive:
                archive.writestr("datapackage.json", "{}")
            log = run_dir / "batch.log"
            log.write_text("log", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "no declared resources"):
                compact_archive_output(run_dir, "x-example", wacz, log)

            self.assertTrue(raw.is_file())
            self.assertTrue(log.is_file())

    def test_compaction_refuses_hash_mismatch_and_truncated_inner_gzip(self):
        for archive_content, archive_hash, expected in (
            (None, "sha256:" + "0" * 64, "failed SHA-256"),
            (b"\x1f\x8btruncated", None, "gzip"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp) / "run"
                collection_dir = run_dir / "collections" / "x-example"
                collection_dir.mkdir(parents=True)
                raw = collection_dir / "raw.warc.gz"
                raw.write_bytes(b"raw")
                wacz = self._write_valid_wacz(
                    collection_dir / "x-example.wacz",
                    archive_content=archive_content,
                    archive_hash=archive_hash,
                )
                log = run_dir / "batch.log"
                log.write_text("log", encoding="utf-8")

                with self.assertRaisesRegex(RuntimeError, expected):
                    compact_archive_output(run_dir, "x-example", wacz, log)

                self.assertTrue(raw.is_file())
                self.assertTrue(log.is_file())

    def _run_fake_archive_execution(
        self,
        root,
        *,
        returncode=0,
        inspection=None,
        retain_working_files=False,
        validation_error=None,
        compaction_error=None,
    ):
        module_dir = Path(root) / "social_media_archive"
        profile = module_dir / "profiles" / "social-auth.tar.gz"
        profile.parent.mkdir(parents=True)
        profile.write_bytes(b"profile")
        batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="x",
            label="example — 2024",
            seed_url="https://x.com/search?q=from%3Aexample",
            collection="x-example-2024",
        )
        inspection = inspection or XCaptureInspection(
            classification="valid",
            search_successes=1,
            search_rate_limits=0,
            search_other_statuses=0,
            rate_limit_remaining=20,
            rate_limit_reset=1786567895,
            error_shell=False,
            detail="valid",
            authentication_state="authenticated",
            authenticated_requests=1,
        )

        def fake_find(run_dir, collection):
            collection_dir = run_dir / "collections" / collection
            (collection_dir / "downloads").mkdir(parents=True)
            (collection_dir / "archive").mkdir()
            (collection_dir / "downloads" / "profile.tar.gz").write_bytes(b"copied profile")
            (collection_dir / "archive" / "raw.warc.gz").write_bytes(b"raw duplicate")
            wacz = collection_dir / f"{collection}.wacz"
            wacz.write_bytes(b"validated package")
            return wacz

        real_compact = browsertrix_archive.compact_archive_output

        def checked_compact(run_dir, *args, **kwargs):
            self.assertTrue((run_dir / "manifest.json").is_file())
            if compaction_error is not None:
                raise compaction_error
            return real_compact(run_dir, *args, **kwargs)

        validator_effect = validation_error if validation_error is not None else None
        verified_auth = XAuthenticationPreflight(
            state="verified",
            verified=True,
            account_handle="crawler_account",
            expected_handle="",
            checked_at="2026-08-25T12:00:00+00:00",
            attempts=1,
            browsertrix_returncode=0,
            profile_refresh_status="refreshed",
            detail="verified",
            reauthentication_required=False,
            reauthentication_command="docker create-login-profile",
            reauthentication_profile_filename="social-auth-reauth.tar.gz",
            ssh_tunnel_command="ssh tunnel",
        )
        with patch.object(browsertrix_archive, "docker_executable", return_value="docker"), patch.object(
            browsertrix_archive, "check_docker"
        ), patch.object(
            browsertrix_archive.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=returncode, stdout="stdout", stderr="stderr"),
        ), patch.object(
            browsertrix_archive, "find_wacz", side_effect=fake_find
        ), patch.object(
            browsertrix_archive, "_validate_wacz_package", side_effect=validator_effect
        ), patch.object(
            browsertrix_archive, "inspect_x_wacz", return_value=inspection
        ), patch.object(
            browsertrix_archive, "compact_archive_output", side_effect=checked_compact
        ) as compact:
            run_dir, results = browsertrix_archive.execute_archive_plan(
                batches=[batch],
                settings=CrawlSettings(retain_working_files=retain_working_files),
                module_data_dir=module_dir,
                profile_path=profile,
                run_id="test-run",
                x_authentication=verified_auth,
            )
        return run_dir, results, compact

    def test_execute_archive_plan_stops_before_capture_when_x_authentication_fails(self):
        batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="x",
            label="example — 2024",
            seed_url="https://x.com/search?q=from%3Aexample",
            collection="x-example-2024",
        )
        failed_auth = XAuthenticationPreflight(
            state="logged_out",
            verified=False,
            account_handle="",
            expected_handle="crawler_account",
            checked_at="2026-08-25T12:00:00+00:00",
            attempts=1,
            browsertrix_returncode=9,
            profile_refresh_status="not_saved",
            detail="The saved X session is logged out.",
            reauthentication_required=True,
            reauthentication_command="docker create-login-profile",
            reauthentication_profile_filename="social-auth-reauth.tar.gz",
            ssh_tunnel_command="ssh tunnel",
        )
        with tempfile.TemporaryDirectory() as tmp:
            module_dir = Path(tmp) / "social_media_archive"
            profile = module_dir / "profiles" / "social-auth.tar.gz"
            profile.parent.mkdir(parents=True)
            profile.write_bytes(b"profile")
            with patch.object(browsertrix_archive, "docker_executable", return_value="docker"), patch.object(
                browsertrix_archive, "check_docker"
            ), patch.object(browsertrix_archive.subprocess, "run") as crawl:
                run_dir, results = browsertrix_archive.execute_archive_plan(
                    batches=[batch],
                    settings=CrawlSettings(expected_x_session_handle="crawler_account"),
                    module_data_dir=module_dir,
                    profile_path=profile,
                    run_id="auth-failure",
                    x_authentication=failed_auth,
                )
            files = sorted(path.name for path in run_dir.iterdir() if path.is_file())
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

        crawl.assert_not_called()
        self.assertEqual(files, ["manifest.json", "plan.json"])
        self.assertEqual(results[0].status, "failed — X reauthentication required")
        self.assertEqual(results[0].validation_status, "authentication_logged_out")
        self.assertEqual(results[0].reauthentication_profile_filename, "social-auth-reauth.tar.gz")
        self.assertEqual(manifest["x_authentication_preflight"]["state"], "logged_out")
        self.assertFalse(manifest["results"][0]["wacz_path"])

    def test_execute_archive_plan_persists_manifest_then_keeps_exact_compact_file_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, results, compact = self._run_fake_archive_execution(tmp)
            files = sorted(str(path.relative_to(run_dir)).replace("\\", "/") for path in run_dir.rglob("*") if path.is_file())
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(
            files,
            ["collections/x-example-2024/x-example-2024.wacz", "manifest.json", "plan.json"],
        )
        compact.assert_called_once()
        self.assertEqual(results[0].status, "completed")
        self.assertEqual(results[0].compaction_status, "compacted")
        self.assertEqual(results[0].crawler_returncode, 0)
        self.assertEqual(manifest["results"][0]["compaction_status"], "compacted")
        self.assertTrue(manifest["results"][0]["crawler_command"])

    def test_execute_archive_plan_compacts_structurally_valid_rate_limit_attempt(self):
        limited = XCaptureInspection(
            classification="rate_limited_empty",
            search_successes=0,
            search_rate_limits=1,
            search_other_statuses=0,
            rate_limit_remaining=0,
            rate_limit_reset=1786567895,
            error_shell=True,
            detail="limited",
            authentication_state="authenticated",
            authenticated_requests=1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, results, compact = self._run_fake_archive_execution(
                tmp, returncode=18, inspection=limited
            )

            self.assertEqual(len([path for path in run_dir.rglob("*") if path.is_file()]), 3)
        compact.assert_called_once()
        self.assertEqual(results[0].status, "rate limited")
        self.assertEqual(results[0].compaction_status, "compacted")

    def test_execute_archive_plan_fails_if_capture_loses_x_authentication(self):
        logged_out = XCaptureInspection(
            classification="authentication_failed",
            search_successes=0,
            search_rate_limits=0,
            search_other_statuses=0,
            rate_limit_remaining=None,
            rate_limit_reset=None,
            error_shell=False,
            detail="login page",
            authentication_state="logged_out",
            login_ui=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            _run_dir, results, compact = self._run_fake_archive_execution(tmp, inspection=logged_out)

        compact.assert_called_once()
        self.assertEqual(results[0].status, "failed — X authentication lost")
        self.assertEqual(results[0].validation_status, "authentication_failed")
        self.assertEqual(results[0].x_session_state, "logged_out")

    def test_execute_archive_plan_fails_closed_without_final_authenticated_evidence(self):
        cases = (
            ("unknown", 0, "failed — X authentication unverified"),
            ("request_only", 0, "failed — X authentication unverified"),
            ("unknown", 1, "failed — X authentication lost"),
        )
        for index, (authentication_state, guest_requests, expected_status) in enumerate(cases):
            with self.subTest(authentication_state=authentication_state, guest_requests=guest_requests):
                inspection = XCaptureInspection(
                    classification="valid",
                    search_successes=1,
                    search_rate_limits=0,
                    search_other_statuses=0,
                    rate_limit_remaining=20,
                    rate_limit_reset=1786567895,
                    error_shell=False,
                    detail="valid search response",
                    authentication_state=authentication_state,
                    authenticated_requests=1 if authentication_state == "request_only" else 0,
                    guest_requests=guest_requests,
                )
                with tempfile.TemporaryDirectory() as tmp:
                    _run_dir, results, compact = self._run_fake_archive_execution(
                        Path(tmp) / f"case-{index}", inspection=inspection
                    )

                compact.assert_called_once()
                self.assertEqual(results[0].status, expected_status)
                self.assertEqual(results[0].validation_status, "authentication_failed")
                self.assertEqual(results[0].x_session_state, authentication_state)
                self.assertIn("create-login-profile", results[0].reauthentication_command)
                self.assertIn("Reopen or reauthenticate", results[0].error)

    def test_execute_archive_plan_treats_exit_18_after_success_as_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, results, compact = self._run_fake_archive_execution(tmp, returncode=18)

            self.assertEqual(len([path for path in run_dir.rglob("*") if path.is_file()]), 3)
        compact.assert_called_once()
        self.assertEqual(results[0].status, "failed — partial rate limited")
        self.assertEqual(results[0].validation_status, "rate_limited_partial")
        self.assertTrue(results[0].partial)

    def test_execute_archive_plan_preserves_exit_18_retry_when_wacz_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, results, compact = self._run_fake_archive_execution(
                tmp,
                returncode=18,
                validation_error=RuntimeError("bad package"),
            )

            self.assertTrue(any(path.suffix == ".log" for path in run_dir.iterdir()))
        compact.assert_not_called()
        self.assertEqual(results[0].status, "rate limited")
        self.assertEqual(results[0].validation_status, "package_invalid")

    def test_execute_archive_plan_retains_workspace_for_invalid_package_or_user_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, invalid_results, compact = self._run_fake_archive_execution(
                Path(tmp) / "invalid", validation_error=RuntimeError("bad package")
            )
            self.assertTrue(any(path.name == "profile.tar.gz" for path in run_dir.rglob("*")))
            self.assertTrue(any(path.suffix == ".log" for path in run_dir.iterdir()))
        compact.assert_not_called()
        self.assertEqual(invalid_results[0].status, "failed")
        self.assertEqual(invalid_results[0].compaction_status, "retained_invalid_package")

        with tempfile.TemporaryDirectory() as tmp:
            run_dir, retained_results, compact = self._run_fake_archive_execution(
                Path(tmp) / "retained", retain_working_files=True
            )
            self.assertTrue(any(path.name == "profile.tar.gz" for path in run_dir.rglob("*")))
        compact.assert_not_called()
        self.assertEqual(retained_results[0].compaction_status, "retained_by_request")

    def test_execute_archive_plan_fails_a_completed_job_when_compaction_cannot_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, results, compact = self._run_fake_archive_execution(
                tmp,
                compaction_error=OSError("locked file"),
            )
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

        compact.assert_called_once()
        self.assertEqual(results[0].status, "failed — compaction incomplete")
        self.assertEqual(results[0].compaction_status, "failed")
        self.assertIn("locked file", results[0].error)
        self.assertEqual(manifest["results"][0]["status"], "failed — compaction incomplete")

    def test_query_settings_normalize_expected_x_session_handle(self):
        settings = social_media_archive._settings(
            {
                "profile_filename": "social-auth.tar.gz",
                "expected_x_session_handle": "@Crawler_Account",
            }
        )

        self.assertEqual(settings.expected_x_session_handle, "Crawler_Account")

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

    def test_result_links_include_the_deployment_url_prefix(self):
        batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="x",
            label="example â€” 2024",
            seed_url="https://x.com/search?q=from%3Aexample",
            collection="x-example-2024",
        )
        result = social_media_archive._planned_result(batch, profile_exists=True)
        form = {"qkey": "social_media_archive", "operation": "plan"}

        with osint_app.app.test_request_context("/run", environ_overrides={"SCRIPT_NAME": "/osint"}):
            rendered = social_media_archive.render_results(
                form, social_media_archive.HEADERS, [social_media_archive._result_row(result)]
            )

        self.assertIn('href="/osint/jobs"', rendered)
        self.assertIn('action="/osint/run"', rendered)

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

    def test_archive_operation_rejects_explicitly_old_x_image_before_queueing(self):
        form = {
            "operation": "archive",
            "x_accounts": "example",
            "x_start": "2024-01-01",
            "x_end": "2024-12-31",
            "batch_mode": "year",
            "profile_filename": "test-profile.tar.gz",
            "browsertrix_image": "webrecorder/browsertrix-crawler:1.13.3",
        }
        with tempfile.TemporaryDirectory() as tmp:
            module_dir = Path(tmp) / "social_media_archive"
            profiles_dir = module_dir / "profiles"
            profiles_dir.mkdir(parents=True)
            (profiles_dir / "test-profile.tar.gz").write_bytes(b"profile")
            with patch.object(social_media_archive, "MODULE_DATA_DIR", module_dir), patch.object(
                social_media_archive, "PROFILES_DIR", profiles_dir
            ), patch.object(social_media_archive, "enqueue_job") as enqueue:
                with self.assertRaisesRegex(ValueError, "1.14.0 or newer"):
                    social_media_archive.run(form)

        enqueue.assert_not_called()

    def test_rate_limited_empty_x_job_is_deferred_with_a_distinct_retry_run_id(self):
        batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="x",
            label="example — 2024",
            seed_url="https://x.com/search?q=from%3Aexample",
            collection="x-example-2024",
        )
        limited = ArchiveResult(
            batch_id=batch.batch_id,
            platform="x",
            label=batch.label,
            target_url=batch.seed_url,
            query_text="from:example",
            period_start="2024-01-01",
            period_end="2024-12-31",
            collection=batch.collection,
            status="rate limited",
            validation_status="rate_limited_empty",
            search_rate_limits=1,
            rate_limit_remaining=0,
            rate_limit_reset=1786567895,
        )
        completed = ArchiveResult(
            batch_id=batch.batch_id,
            platform="x",
            label=batch.label,
            target_url=batch.seed_url,
            query_text="from:example",
            period_start="2024-01-01",
            period_end="2024-12-31",
            collection=batch.collection,
            status="completed",
            validation_status="valid",
            search_successes=1,
            rate_limit_remaining=20,
        )
        payload = {
            "batch": asdict(batch),
            "settings": asdict(CrawlSettings()),
            "profile_filename": "social-auth.tar.gz",
            "run_id": "archive-test-batch",
        }
        run_ids = []

        def execute(**kwargs):
            run_ids.append(kwargs["run_id"])
            result = limited if len(run_ids) == 1 else completed
            return Path("C:/tmp") / kwargs["run_id"], [result]

        with patch.object(social_media_archive, "execute_archive_plan", side_effect=execute), patch.object(
            social_media_archive, "_x_retry_at", return_value="2026-08-12T20:51:50+00:00"
        ), patch.object(social_media_archive, "set_job_throttle"):
            with self.assertRaises(social_media_archive.JobRetry) as deferred:
                social_media_archive.run_queued_job(payload, lambda *args: None)
            result = social_media_archive.run_queued_job(deferred.exception.payload, lambda *args: None)

        self.assertEqual(run_ids, ["archive-test-batch", "archive-test-batch-attempt-2"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(len(result["previous_attempts"]), 1)

    def test_retry_provenance_is_written_into_the_existing_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            manifest_path = run_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps({"results": [{"batch_id": "batch-0001", "status": "rate limited"}]}),
                encoding="utf-8",
            )
            social_media_archive._persist_attempt_manifest(
                run_dir,
                {
                    "batch_id": "batch-0001",
                    "status": "rate limited — retry scheduled",
                    "attempt_count": 2,
                    "retry_at": "2026-08-12T20:51:50+00:00",
                    "previous_attempts": [{"run_id": "first"}],
                },
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["results"][0]["status"], "rate limited — retry scheduled")
        self.assertEqual(manifest["job_attempt"]["attempt_number"], 2)
        self.assertEqual(manifest["job_attempt"]["previous_attempts"], [{"run_id": "first"}])

    def test_partial_x_job_is_failed_and_not_blindly_retried(self):
        batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="x",
            label="example — 2024",
            seed_url="https://x.com/search?q=from%3Aexample",
            collection="x-example-2024",
        )
        partial = ArchiveResult(
            batch_id=batch.batch_id,
            platform="x",
            label=batch.label,
            target_url=batch.seed_url,
            query_text="from:example",
            period_start="2024-01-01",
            period_end="2024-12-31",
            collection=batch.collection,
            status="failed — partial rate limited",
            validation_status="rate_limited_partial",
            search_successes=12,
            search_rate_limits=1,
            rate_limit_remaining=0,
            partial=True,
        )
        payload = {
            "batch": asdict(batch),
            "settings": asdict(CrawlSettings()),
            "profile_filename": "social-auth.tar.gz",
            "run_id": "archive-partial",
        }
        with patch.object(
            social_media_archive,
            "execute_archive_plan",
            return_value=(Path("C:/tmp/archive-partial"), [partial]),
        ), patch.object(
            social_media_archive, "_x_retry_at", return_value="2026-08-12T20:51:50+00:00"
        ), patch.object(social_media_archive, "set_job_throttle") as throttle:
            with self.assertRaisesRegex(social_media_archive.JobExecutionError, "partial"):
                social_media_archive.run_queued_job(payload, lambda *args: None)

        throttle.assert_called_once()

    def test_non_rate_invalid_partial_x_job_does_not_set_rate_limit_throttle(self):
        batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="x",
            label="example — 2024",
            seed_url="https://x.com/search?q=from%3Aexample",
            collection="x-example-2024",
        )
        partial = ArchiveResult(
            batch_id=batch.batch_id,
            platform="x",
            label=batch.label,
            target_url=batch.seed_url,
            query_text="from:example",
            period_start="2024-01-01",
            period_end="2024-12-31",
            collection=batch.collection,
            status="failed",
            validation_status="invalid_partial",
            search_successes=1,
            search_other_statuses=1,
            partial=True,
            error="SearchTimeline returned a malformed response.",
        )
        payload = {
            "batch": asdict(batch),
            "settings": asdict(CrawlSettings()),
            "profile_filename": "social-auth.tar.gz",
            "run_id": "archive-invalid-partial",
        }
        with patch.object(
            social_media_archive,
            "execute_archive_plan",
            return_value=(Path("C:/tmp/archive-invalid-partial"), [partial]),
        ), patch.object(social_media_archive, "set_job_throttle") as throttle:
            with self.assertRaisesRegex(social_media_archive.JobExecutionError, "malformed"):
                social_media_archive.run_queued_job(payload, lambda *args: None)

        throttle.assert_not_called()

    def test_x_rate_limit_retries_are_bounded(self):
        batch = ArchiveBatch(
            batch_id="batch-0001",
            platform="x",
            label="example — 2024",
            seed_url="https://x.com/search?q=from%3Aexample",
            collection="x-example-2024",
        )
        limited = ArchiveResult(
            batch_id=batch.batch_id,
            platform="x",
            label=batch.label,
            target_url=batch.seed_url,
            query_text="from:example",
            period_start="2024-01-01",
            period_end="2024-12-31",
            collection=batch.collection,
            status="rate limited",
            validation_status="rate_limited_empty",
            search_rate_limits=1,
        )
        payload = {
            "batch": asdict(batch),
            "settings": asdict(CrawlSettings()),
            "profile_filename": "social-auth.tar.gz",
            "run_id": "archive-limited",
            "rate_limit_retry_count": social_media_archive.X_RATE_LIMIT_MAX_RETRIES,
        }
        with patch.object(
            social_media_archive,
            "execute_archive_plan",
            return_value=(Path("C:/tmp/archive-limited"), [limited]),
        ), patch.object(
            social_media_archive, "_x_retry_at", return_value="2026-08-12T20:51:50+00:00"
        ), patch.object(social_media_archive, "set_job_throttle"):
            with self.assertRaisesRegex(social_media_archive.JobExecutionError, "remained rate limited"):
                social_media_archive.run_queued_job(payload, lambda *args: None)

    def test_x_retry_time_never_falls_in_the_past(self):
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        retry_at = datetime.fromisoformat(social_media_archive._x_retry_at(now_epoch - 5 * 60))

        self.assertGreaterEqual(int(retry_at.timestamp()), now_epoch + social_media_archive.X_RATE_LIMIT_GRACE_SECONDS)
        self.assertLess(int(retry_at.timestamp()), now_epoch + 60)

    def test_automation_profile_review_queues_content_only_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            module_dir = Path(tmp) / "social_media_archive"
            profiles_dir = module_dir / "profiles"
            profiles_dir.mkdir(parents=True)
            (profiles_dir / "social-auth.tar.gz").write_bytes(b"profile")
            with patch.object(social_media_archive, "MODULE_DATA_DIR", module_dir), patch.object(
                social_media_archive, "PROFILES_DIR", profiles_dir
            ), patch.object(social_media_archive, "enqueue_job", return_value="a" * 32) as enqueue:
                result = social_media_archive.enqueue_profile_review(
                    {
                        "platform": "x",
                        "profile": "example",
                        "lookback": {"value": 2, "unit": "weeks"},
                        "end_date": "2026-08-01",
                    }
                )

        self.assertEqual(result["requested_start"], "2026-07-19")
        self.assertEqual(result["requested_end"], "2026-08-01")
        self.assertEqual(result["date_filter"], "enforced")
        payload = enqueue.call_args.kwargs["payload"]
        self.assertFalse(payload["settings"]["save_final_screenshot"])
        self.assertTrue(payload["settings"]["extract_final_text"])
        self.assertEqual(payload["settings"]["behaviors"], "autoscroll,autoplay,autofetch")
        self.assertIn("since:2026-07-19 until:2026-08-02", payload["batch"]["query_text"])

    def test_wacz_content_bundle_extracts_final_text_and_graphics(self):
        def warc_record(record_type, target, content_type, block):
            headers = (
                "WARC/1.1\r\n"
                f"WARC-Type: {record_type}\r\n"
                f"WARC-Target-URI: {target}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(block)}\r\n\r\n"
            ).encode("utf-8")
            return headers + block + b"\r\n\r\n"

        page_url = "https://x.com/example/status/123"
        text_record = warc_record("resource", f"urn:textFinal:{page_url}", "text/plain", b"Final post text")
        image_body = b"\x89PNG\r\n\x1a\n" + b"image-data"
        http_block = (
            b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: "
            + str(len(image_body)).encode("ascii")
            + b"\r\n\r\n"
            + image_body
        )
        image_record = warc_record("response", "https://pbs.twimg.com/media/test.png", "application/http", http_block)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wacz = root / "test.wacz"
            with zipfile.ZipFile(wacz, "w") as archive:
                archive.writestr(
                    "pages/pages.jsonl",
                    json.dumps({"format": "json-pages-1.0"})
                    + "\n"
                    + json.dumps({"url": page_url, "ts": "2026-08-01T12:00:00Z", "title": "Example"})
                    + "\n",
                )
                archive.writestr("archive/data.warc.gz", gzip.compress(text_record + image_record))
            details = extract_wacz_content(wacz, root / "content")
            bundle = json.loads(Path(details["content_path"]).read_text(encoding="utf-8"))
            media_path = root / "content" / bundle["media"][0]["file"]

            self.assertEqual(bundle["documents"][0]["text"], "Final post text")
            self.assertEqual(bundle["media_count"], 1)
            self.assertEqual(media_path.read_bytes(), image_body)


if __name__ == "__main__":
    unittest.main()
