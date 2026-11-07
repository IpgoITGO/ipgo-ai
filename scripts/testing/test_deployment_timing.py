import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("summarize_timing", ROOT / "scripts/deployment/summarize_timing.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DeploymentTimingTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "GITHUB_REPOSITORY": "example/project",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_JOB": "deploy",
            "DEPLOY_STATUS": "failure",
            "IMAGE_REUSED": "true",
        }
        self.run = {"run_attempt": 1, "created_at": "2026-11-07T00:00:00Z", "run_started_at": "2026-11-07T01:00:00Z"}
        self.job = {
            "name": "deploy",
            "run_attempt": 1,
            "started_at": "2026-11-07T00:10:00Z",
            "steps": [
                {"name": "Build and push immutable image", "status": "completed", "conclusion": "skipped"},
                {
                    "name": "Deploy task definition",
                    "status": "completed",
                    "conclusion": "failure",
                    "started_at": "2026-11-07T00:10:01Z",
                    "completed_at": "2026-11-07T00:12:01Z",
                },
                {"name": "Summarize deployment timing", "status": "in_progress"},
            ],
        }

    def report(self):
        return MODULE.render(self.run, [self.job], self.env, "2026-11-07T00:12:10Z")

    def test_wait_failure_reused_and_skipped_are_distinct(self):
        text = self.report()
        for value in [
            "600초",
            "130초",
            "120초",
            "failure",
            "기존 SHA 이미지 재사용",
            "| Build and push immutable image | — | 생략 |",
        ]:
            self.assertIn(value, text)
        self.assertNotIn("| Summarize deployment timing", text)

    def test_rerun_uses_current_attempt_start(self):
        self.env["GITHUB_RUN_ATTEMPT"] = "2"
        self.run["run_attempt"] = self.job["run_attempt"] = 2
        self.job["started_at"] = "2026-11-07T01:02:00Z"
        self.assertIn("→ job 시작: **120초**", self.report())

    def test_missing_and_in_progress_never_become_zero(self):
        step = self.job["steps"][1]
        step["completed_at"] = None
        self.assertIn("| 미확인 | failure |", self.report())
        step["status"] = "in_progress"
        self.assertIn("| 미확인 | API 집계 중 |", self.report())

    def test_invalid_time_is_not_negative_duration(self):
        self.assertEqual(MODULE.elapsed(None, None), "미확인")
        self.assertEqual(MODULE.elapsed("2026-11-07T01:00:00Z", "2026-11-07T00:00:00Z"), "미확인")

    def test_ambiguous_or_missing_job_is_rejected(self):
        for jobs in [[], [self.job, copy.deepcopy(self.job)]]:
            with self.assertRaises(ValueError):
                MODULE.render(self.run, jobs, self.env, "2026-11-07T00:12:10Z")

    def test_collect_paginates_and_isolates_attempt(self):
        calls = []

        def get(url):
            calls.append(url)
            if "/jobs?" not in url:
                return self.run
            return {"jobs": [{}] * 100 if url.endswith("&page=1") else [self.job]}

        _, jobs = MODULE.collect(self.env, get)
        self.assertEqual(len(jobs), 101)
        self.assertIn("/attempts/1/jobs?", calls[1])
        self.run["run_attempt"] = 2
        with self.assertRaises(ValueError):
            MODULE.collect(self.env, get)

    def test_api_failure_is_nonfatal_and_does_not_leak_exception(self):
        def get(_):
            raise RuntimeError("fixture-secret-token")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.md"
            self.env["GITHUB_STEP_SUMMARY"] = str(path)
            self.assertEqual(MODULE.main(self.env, get), 0)
            text = path.read_text()
            self.assertIn("조회 불가", text)
            self.assertNotIn("fixture-secret-token", text)

    def test_success_writes_to_summary_without_external_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.md"
            self.env.update(GITHUB_STEP_SUMMARY=str(path), DEPLOY_STATUS="success", IMAGE_REUSED="false")
            responses = iter([self.run, {"jobs": [self.job]}])
            MODULE.main(self.env, lambda _: next(responses), "2026-11-07T00:12:10Z")
            self.assertIn("`success`", path.read_text())
            self.assertIn("새 이미지 빌드 경로", path.read_text())


if __name__ == "__main__":
    unittest.main()
