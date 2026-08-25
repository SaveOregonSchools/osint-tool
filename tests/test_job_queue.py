import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import job_queue


class JobQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "jobs.sqlite"
        self.db_patch = patch.object(job_queue, "JOB_DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        job_queue._initialized_paths.discard(str(self.db_path.resolve()))

    def test_queued_job_runs_and_persists_result(self):
        job_queue.register_job_handler(
            "test.success",
            lambda payload, update: (update(1, 1, "done") or {"value": payload["value"]}),
        )
        job_id = job_queue.enqueue_job(
            module_key="test",
            handler_key="test.success",
            label="Successful job",
            payload={"value": 42},
            start_worker_now=False,
        )

        self.assertTrue(job_queue.run_pending_job_once())
        job = job_queue.get_job(job_id)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["value"], 42)
        self.assertEqual(job["progress_current"], 1)

    def test_job_failure_retains_structured_result(self):
        def fail(payload, update):
            raise job_queue.JobExecutionError("capture failed", {"output_dir": "somewhere"})

        job_queue.register_job_handler("test.failure", fail)
        job_id = job_queue.enqueue_job(
            module_key="test",
            handler_key="test.failure",
            label="Failed job",
            payload={},
            start_worker_now=False,
        )

        job_queue.run_pending_job_once()
        job = job_queue.get_job(job_id)

        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "capture failed")
        self.assertEqual(job["result"]["output_dir"], "somewhere")

    def test_interrupted_running_jobs_are_recovered_as_failed(self):
        job_queue.init_job_queue()
        with job_queue._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, group_id, module_key, handler_key, label, status,
                    payload_json, created_at
                ) VALUES ('one', 'group', 'test', 'test.none', 'Interrupted', 'running', '{}', '2026-01-01')
                """
            )
        job_queue._initialized_paths.discard(str(self.db_path.resolve()))

        job_queue.init_job_queue()
        job = job_queue.get_job("one")

        self.assertEqual(job["status"], "failed")
        self.assertIn("queue worker stopped", job["error"])

    def test_jobs_are_listed_in_execution_order_before_recent_history(self):
        job_queue.init_job_queue()
        records = [
            ("completed-old", "completed", "2026-01-01T10:00:00+00:00", "2026-01-01T10:10:00+00:00"),
            ("queued-second", "queued", "2026-01-01T10:03:00+00:00", ""),
            ("running", "running", "2026-01-01T10:04:00+00:00", ""),
            ("queued-first", "queued", "2026-01-01T10:02:00+00:00", ""),
            ("completed-new", "completed", "2026-01-01T10:05:00+00:00", "2026-01-01T10:20:00+00:00"),
        ]
        with job_queue._connect() as conn:
            conn.executemany(
                """
                INSERT INTO jobs (
                    id, group_id, module_key, handler_key, label, status,
                    payload_json, created_at, completed_at
                ) VALUES (?, 'group', 'test', 'test.none', ?, ?, '{}', ?, ?)
                """,
                [(job_id, job_id, status, created_at, completed_at) for job_id, status, created_at, completed_at in records],
            )

        jobs = job_queue.list_jobs()

        self.assertEqual(
            [job["id"] for job in jobs],
            ["running", "queued-first", "queued-second", "completed-new", "completed-old"],
        )

    def test_legacy_database_is_migrated_for_deferred_jobs(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    module_key TEXT NOT NULL,
                    handler_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 1,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                INSERT INTO jobs (id, group_id, module_key, handler_key, label, status, payload_json, created_at)
                VALUES ('legacy', 'group', 'test', 'test.none', 'Legacy', 'queued', '{}', '2026-01-01')
                """
            )
        conn.close()

        job_queue.init_job_queue()

        with job_queue._connect() as conn:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)")}
        self.assertTrue(
            {"available_at", "throttle_key", "resource_key", "attempt_count", "worker_id", "heartbeat_at"}.issubset(
                columns
            )
        )
        self.assertEqual(job_queue.get_job("legacy")["status"], "queued")

    def test_deferred_throttle_skips_siblings_but_runs_unrelated_work(self):
        retry_at = "2026-01-01T10:15:00+00:00"
        calls = []

        def throttled(payload, update):
            calls.append(payload.get("attempt", 0))
            if not payload.get("attempt"):
                raise job_queue.JobRetry(
                    "Rate limited; retry later",
                    retry_at=retry_at,
                    payload={"attempt": 1},
                    result={"status": "rate limited"},
                    throttle_key="x:profile",
                )
            return {"status": "completed after retry"}

        job_queue.register_job_handler("test.throttled", throttled)
        job_queue.register_job_handler("test.unrelated", lambda payload, update: {"status": "completed"})
        with patch.object(job_queue, "_now", return_value="2026-01-01T10:00:00+00:00"):
            first = job_queue.enqueue_job(
                module_key="test",
                handler_key="test.throttled",
                label="First X job",
                payload={},
                throttle_key="x:profile",
                start_worker_now=False,
            )
            sibling = job_queue.enqueue_job(
                module_key="test",
                handler_key="test.throttled",
                label="Sibling X job",
                payload={"attempt": 1},
                throttle_key="x:profile",
                start_worker_now=False,
            )
            unrelated = job_queue.enqueue_job(
                module_key="test",
                handler_key="test.unrelated",
                label="Unrelated job",
                payload={},
                start_worker_now=False,
            )
            self.assertTrue(job_queue.run_pending_job_once())
            self.assertEqual(job_queue.get_job(first)["status"], "queued")
            self.assertEqual(job_queue.get_job(first)["attempt_count"], 1)
            self.assertEqual(job_queue.get_job(first)["result"]["status"], "rate limited")
            self.assertTrue(job_queue.run_pending_job_once())

        self.assertEqual(job_queue.get_job(unrelated)["status"], "completed")
        self.assertEqual(job_queue.get_job(sibling)["status"], "queued")
        with patch.object(job_queue, "_now", return_value="2026-01-01T10:15:00+00:00"):
            self.assertTrue(job_queue.run_pending_job_once())

        self.assertEqual(job_queue.get_job(first)["status"], "completed")
        self.assertEqual(job_queue.get_job(first)["attempt_count"], 2)
        self.assertEqual(calls, [0, 1])

    def test_queue_timestamps_are_normalized_to_utc_and_naive_values_are_rejected(self):
        retry = job_queue.JobRetry(
            "later",
            retry_at="2026-01-01T05:30:00-05:00",
            payload={},
        )
        self.assertEqual(retry.retry_at, "2026-01-01T10:30:00+00:00")
        with self.assertRaisesRegex(ValueError, "timezone"):
            job_queue.JobRetry("later", retry_at="2026-01-01T10:30:00", payload={})

        job_queue.register_job_handler("test.timestamp", lambda payload, update: {})
        job_id = job_queue.enqueue_job(
            module_key="test",
            handler_key="test.timestamp",
            label="Timestamp",
            payload={},
            available_at="2026-01-01T05:30:00-05:00",
            start_worker_now=False,
        )
        self.assertEqual(job_queue.get_job(job_id)["available_at"], "2026-01-01T10:30:00+00:00")

    def test_same_throttle_key_cannot_be_claimed_while_one_is_running(self):
        job_queue.register_job_handler("test.claim", lambda payload, update: {})
        first = job_queue.enqueue_job(
            module_key="test",
            handler_key="test.claim",
            label="First",
            payload={},
            throttle_key="x:profile",
            start_worker_now=False,
        )
        sibling = job_queue.enqueue_job(
            module_key="test",
            handler_key="test.claim",
            label="Sibling",
            payload={},
            throttle_key="x:profile",
            start_worker_now=False,
        )

        self.assertEqual(job_queue._claim_next_job()["id"], first)
        self.assertIsNone(job_queue._claim_next_job())
        self.assertEqual(job_queue.get_job(sibling)["status"], "queued")

    def test_same_resource_key_serializes_jobs_with_different_throttles(self):
        job_queue.register_job_handler("test.resource", lambda payload, update: {})
        first = job_queue.enqueue_job(
            module_key="test",
            handler_key="test.resource",
            label="First",
            payload={},
            throttle_key="x:first",
            resource_key="browsertrix",
            start_worker_now=False,
        )
        sibling = job_queue.enqueue_job(
            module_key="test",
            handler_key="test.resource",
            label="Sibling",
            payload={},
            throttle_key="x:second",
            resource_key="browsertrix",
            start_worker_now=False,
        )

        self.assertEqual(job_queue._claim_next_job()["id"], first)
        self.assertIsNone(job_queue._claim_next_job())
        self.assertEqual(job_queue.get_job(sibling)["status"], "queued")

    def test_defer_persists_new_throttle_key_and_never_shortens_existing_gate(self):
        job_queue.register_job_handler(
            "test.add-throttle",
            lambda payload, update: (_ for _ in ()).throw(
                job_queue.JobRetry(
                    "limited",
                    retry_at="2026-01-01T10:30:00+00:00",
                    payload={"retried": True},
                    throttle_key="x:new-profile",
                )
            ),
        )
        job_id = job_queue.enqueue_job(
            module_key="test",
            handler_key="test.add-throttle",
            label="Add throttle",
            payload={},
            start_worker_now=False,
        )
        with patch.object(job_queue, "_now", return_value="2026-01-01T10:00:00+00:00"):
            self.assertTrue(job_queue.run_pending_job_once())
            job_queue.set_job_throttle("x:new-profile", "2026-01-01T10:20:00+00:00", "shorter")

        self.assertEqual(job_queue.get_job(job_id)["throttle_key"], "x:new-profile")
        with job_queue._connect() as conn:
            throttle = conn.execute(
                "SELECT blocked_until, message FROM job_throttles WHERE throttle_key = 'x:new-profile'"
            ).fetchone()
        self.assertEqual(throttle["blocked_until"], "2026-01-01T10:30:00+00:00")
        self.assertEqual(throttle["message"], "limited")
        self.assertEqual(job_queue.get_job(job_id)["message"], "limited")

    def test_fresh_foreign_worker_heartbeat_is_not_recovered_until_it_is_stale(self):
        job_queue.register_job_handler("test.foreign", lambda payload, update: {})
        with patch.object(job_queue, "_now", return_value="2026-01-01T10:00:00+00:00"):
            job_queue.init_job_queue()
            with job_queue._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        id, group_id, module_key, handler_key, label, status,
                        payload_json, throttle_key, worker_id, heartbeat_at, created_at
                    ) VALUES (
                        'foreign', 'group', 'test', 'test.foreign', 'Foreign', 'running',
                        '{}', 'x:profile', 'another-process', '2026-01-01T10:00:00+00:00',
                        '2026-01-01T09:59:00+00:00'
                    )
                    """
                )
            sibling = job_queue.enqueue_job(
                module_key="test",
                handler_key="test.foreign",
                label="Sibling",
                payload={},
                throttle_key="x:profile",
                start_worker_now=False,
            )

        job_queue._initialized_paths.discard(str(self.db_path.resolve()))
        with patch.object(job_queue, "_now", return_value="2026-01-01T10:00:30+00:00"):
            job_queue.init_job_queue()
            self.assertEqual(job_queue.get_job("foreign")["status"], "running")
            self.assertIsNone(job_queue._claim_next_job())

        with patch.object(job_queue, "_now", return_value="2026-01-01T10:02:00+00:00"):
            claimed = job_queue._claim_next_job()

        self.assertEqual(job_queue.get_job("foreign")["status"], "failed")
        self.assertEqual(claimed["id"], sibling)


if __name__ == "__main__":
    unittest.main()
