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
        self.assertIn("application stopped", job["error"])


if __name__ == "__main__":
    unittest.main()
