import contextlib
import copy
import importlib.util
import io
import json
import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("verification", ROOT / "scripts/deployment/verify_deployment.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
SHA = "a" * 40
HEAD = "b" * 40
ROOT_DIGEST = "sha256:" + "c" * 64
LEAF = "sha256:" + "d" * 64
OTHER = "sha256:" + "e" * 64
ACCOUNT = "123456789012"


class CiVerificationTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "GITHUB_REPOSITORY": "IpgoITGO/ipgo-server",
            "EXPECTED_SHA": SHA,
            "DEPLOYMENT_SOURCE_SHA": SHA,
            "DESIRED_COUNT": "1",
            "API_DESIRED_COUNT": "1",
            "DEPLOYMENT_SMOKE_URL": "https://api.example.invalid/actuator/health",
        }
        self.run = {
            "id": 10,
            "head_sha": SHA,
            "event": "push",
            "path": ".github/workflows/test.yml",
            "head_repository": {"full_name": "IpgoITGO/ipgo-server"},
            "head_branch": "dev",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 2,
        }
        self.jobs = [
            {
                "name": name,
                "run_attempt": 2,
                "status": "completed",
                "conclusion": "success",
            }
            for name in sorted(M.JOBS["server"])
        ]
        self.runs = [self.run]
        self.db_run = {
            **self.run,
            "id": 20,
            "head_sha": HEAD,
            "event": "pull_request",
            "path": ".github/workflows/database.yml",
            "head_branch": "feature/db",
        }
        self.db_jobs = [
            {
                "name": "verify",
                "run_attempt": 2,
                "status": "completed",
                "conclusion": "success",
            }
        ]
        self.prs = [
            {
                "number": 1,
                "merged_at": "2026-11-07T00:00:00Z",
                "merge_commit_sha": SHA,
                "base": {"ref": "dev"},
                "head": {"sha": HEAD, "repo": {"full_name": "IpgoITGO/ipgo-server"}},
            }
        ]
        self.pr_files = ["README.md"]
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if "/workflows/test.yml/" in path:
            return {"workflow_runs": self.runs}
        if "/workflows/database.yml/" in path:
            return {"workflow_runs": [self.db_run]}
        if "/runs/10/" in path:
            return {"jobs": self.jobs}
        if "/runs/20/" in path:
            return {"jobs": self.db_jobs}
        if "/pulls/1/files" in path:
            return [name if isinstance(name, dict) else {"filename": name} for name in self.pr_files]
        if "/commits/" in path:
            return self.prs
        raise AssertionError("Unexpected evidence query")

    def check(self, files=()):
        return M.ci_check(self.env, "server", self.get, files)

    def test_success_is_exact_source_push_and_current_attempt(self):
        self.assertEqual(self.check(), {"test_run": 10})
        self.assertIn("head_sha=" + SHA, self.calls[0])
        self.assertTrue(any("/attempts/2/jobs" in path for path in self.calls))

    def test_newer_non_success_never_falls_back_to_green(self):
        for status, conclusion in [
            ("completed", "failure"),
            ("completed", "cancelled"),
            ("in_progress", None),
        ]:
            self.runs = [
                self.run,
                {**self.run, "id": 11, "status": status, "conclusion": conclusion},
            ]
            with self.subTest(status=status), self.assertRaises(M.GateError):
                self.check()

    def test_wrong_ci_source_event_branch_workflow_or_repository(self):
        changes = [
            {"head_sha": HEAD},
            {"event": "pull_request"},
            {"head_branch": "main"},
            {"path": ".github/workflows/other.yml"},
            {"head_repository": {"full_name": "fork/project"}},
        ]
        for change in changes:
            self.runs = [{**self.run, **change}]
            with self.subTest(change=change), self.assertRaises(M.GateError):
                self.check()

    def test_recent_failed_rerun_of_an_older_run_does_not_use_later_green_id(self):
        self.run["updated_at"] = "2026-11-07T00:00:00Z"
        self.runs = [self.run, {**self.run, "id": 9, "conclusion": "failure", "updated_at": "2026-11-07T01:00:00Z"}]
        with self.assertRaises(M.GateError):
            self.check()

    def test_missing_ci_run_is_not_success(self):
        self.runs = []
        with self.assertRaises(M.GateError):
            self.check()

    def test_required_jobs_missing_skipped_failed_or_wrong_attempt(self):
        original = copy.deepcopy(self.jobs)
        for change in [
            {"conclusion": "skipped"},
            {"conclusion": "failure"},
            {"status": "in_progress"},
            {"run_attempt": 1},
        ]:
            self.jobs = copy.deepcopy(original)
            self.jobs[0].update(change)
            with self.subTest(change=change), self.assertRaises(M.GateError):
                self.check()
        for jobs in [original[:-1], original + [original[0]]]:
            self.jobs = jobs
            with self.assertRaises(M.GateError):
                self.check()

    def test_wrong_approved_source_stops_before_api(self):
        self.env["DEPLOYMENT_SOURCE_SHA"] = HEAD
        with self.assertRaises(M.GateError):
            self.check()
        self.assertEqual(self.calls, [])

    def test_database_evidence_is_bound_to_exact_merged_pr_head(self):
        result = self.check(["scripts/testing/verify-monthly-insight-budget.sh"])
        self.assertEqual(result, {"test_run": 10, "database_run": 20})
        self.assertTrue(any("head_sha=" + HEAD in path for path in self.calls))

    def test_database_failure_or_unrelated_pr_rejects(self):
        self.db_jobs[0]["conclusion"] = "failure"
        with self.assertRaises(M.GateError):
            self.check(["src/main/resources/db/migration/V1.sql"])
        self.db_jobs[0]["conclusion"] = "success"
        self.prs[0]["merge_commit_sha"] = HEAD
        with self.assertRaises(M.GateError):
            self.check(["build.gradle"])

    def test_database_change_in_earlier_pr_commit_is_not_missed(self):
        self.pr_files = ["scripts/testing/verify-monthly-insight-budget.sh", "README.md"]
        self.assertEqual(self.check(["README.md"])["database_run"], 20)
        self.db_jobs[0]["conclusion"] = "failure"
        with self.assertRaises(M.GateError):
            self.check(["README.md"])

    def test_document_only_change_does_not_require_database_workflow(self):
        self.assertEqual(self.check(["README.md", "docs/guide.md"]), {"test_run": 10})
        self.assertFalse(any("database.yml" in path for path in self.calls))

    def test_renamed_db_input_requires_exact_successful_database_evidence(self):
        old = "src/main/resources/db/migration/V999__fixture.sql"
        new = "docs/archived-migration.sql"
        self.pr_files = [{"filename": new, "previous_filename": old, "status": "renamed"}]
        self.assertEqual(self.check([new]), {"test_run": 10, "database_run": 20})
        for conclusion in ("failure", "cancelled", None):
            self.db_run["conclusion"] = conclusion
            with self.subTest(conclusion=conclusion), self.assertRaises(M.GateError):
                self.check([new])
        original = self.get

        def missing_database(path):
            if "/workflows/database.yml/" in path:
                return {"workflow_runs": []}
            return original(path)

        with self.assertRaises(M.GateError):
            M.ci_check(self.env, "server", missing_database, [new])

    def test_rename_into_db_deletion_and_mixed_paths_require_database(self):
        cases = [
            [{"filename": "database/probe.sql", "previous_filename": "docs/probe.sql", "status": "renamed"}],
            [{"filename": "database/probe.sql", "status": "removed"}],
            [{"filename": "README.md"}, {"filename": "docs/probe.sql", "previous_filename": "database/probe.sql"}],
        ]
        for files in cases:
            with self.subTest(files=files):
                self.pr_files = files
                self.assertIn("database_run", self.check())
                self.db_run["conclusion"] = "failure"
                with self.assertRaises(M.GateError):
                    self.check()
                self.db_run["conclusion"] = "success"

    def test_non_database_rename_does_not_add_database_requirement(self):
        self.pr_files = [{"filename": "docs/new.md", "previous_filename": "docs/old.md", "status": "renamed"}]
        self.assertEqual(self.check(["docs/new.md"]), {"test_run": 10})
        self.assertFalse(any("/workflows/database.yml/" in path for path in self.calls))

    def test_malformed_previous_file_path_is_not_silently_ignored(self):
        for previous in (None, "", 1):
            self.pr_files = [{"filename": "docs/new.md", "previous_filename": previous}]
            with self.subTest(previous=previous), self.assertRaises(M.GateError):
                self.check()

    def test_cache_and_deployment_tools_do_not_require_unrelated_database_ci(self):
        # PR143's actual file set previously blocked redeployment of dev314bb98.
        files = [
            ".github/workflows/deploy-dev.yml",
            ".github/workflows/test.yml",
            "Dockerfile",
            "README.md",
            "scripts/deployment/cache-dependencies.gradle",
            "scripts/deployment/summarize_timing.py",
            "scripts/testing/test_deployment_timing.py",
        ]
        self.pr_files = files
        self.db_run["conclusion"] = "failure"
        self.assertEqual(self.check(files), {"test_run": 10})
        self.assertFalse(any("database.yml" in path for path in self.calls))

    def test_real_database_inputs_still_require_successful_database_ci(self):
        for path in [
            "scripts/testing/verify-monthly-insight-budget.sh",
            "scripts/database-task.sh",
            "scripts/testing/billing-ledger-invariants.sql",
            "database/bootstrap/create_roles.sql",
            "src/main/resources/db/migration/V99.sql",
            "build.gradle",
            "settings.gradle",
            "gradle/wrapper/gradle-wrapper.properties",
            ".github/workflows/database.yml",
        ]:
            self.pr_files = [path]
            self.calls.clear()
            with self.subTest(path=path):
                self.db_run["conclusion"] = "success"
                self.assertEqual(self.check([])["database_run"], 20)
                self.db_run["conclusion"] = "failure"
                with self.assertRaises(M.GateError):
                    self.check([])

    def test_database_change_mixed_with_cache_work_is_not_exempt(self):
        self.pr_files = ["scripts/deployment/summarize_timing.py", "src/main/resources/db/migration/V99.sql"]
        self.db_run["conclusion"] = "failure"
        with self.assertRaises(M.GateError):
            self.check(["README.md"])

    def test_ai_uses_its_own_required_job_names(self):
        self.env.update(GITHUB_REPOSITORY="IpgoITGO/ipgo-ai", DEPLOYMENT_SMOKE_URL="")
        self.run["head_repository"]["full_name"] = "IpgoITGO/ipgo-ai"
        self.jobs = [
            {
                "name": name,
                "run_attempt": 2,
                "status": "completed",
                "conclusion": "success",
            }
            for name in M.JOBS["ai"]
        ]
        self.assertEqual(
            M.ci_check(self.env, "ai", self.get, ["scripts/testing/test_sample.py"]),
            {"test_run": 10},
        )

    def test_missing_server_smoke_url_rejected_except_scale_to_zero(self):
        self.env["DEPLOYMENT_SMOKE_URL"] = ""
        with self.assertRaises(M.GateError):
            self.check()
        self.env["DESIRED_COUNT"] = "0"
        self.assertEqual(self.check(), {"test_run": 10})

    def test_incomplete_pagination_rejects(self):
        with self.assertRaises(M.GateError):
            M.pages(lambda _: [{}] * 100, "/fixture")

    def test_paginated_jobs_are_not_truncated(self):
        calls = []

        def get(path):
            calls.append(path)
            return {"jobs": [{"name": "other"}] * 100 if path.endswith("page=1") else self.jobs}

        self.assertEqual(len(M.pages(get, "/fixture", "jobs")), 104)
        self.assertEqual(len(calls), 2)


class RuntimeVerificationTests(unittest.TestCase):
    def setUp(self):
        self.name = "ipgo-dev-onebox-server"
        self.td = f"arn:aws:ecs:ap-northeast-2:{ACCOUNT}:task-definition/{self.name}:30"
        self.image = f"{ACCOUNT}.dkr.ecr.ap-northeast-2.amazonaws.com/ipgo-dev/server:{SHA}-arm64"
        self.env = {
            "AWS_ACCOUNT_ID": ACCOUNT,
            "COMPUTE_MODE": "onebox",
            "ECS_SERVICE_NAME": self.name,
            "EXPECTED_SHA": SHA,
            "EXPECTED_TASK_DEFINITION": self.td,
            "EXPECTED_IMAGE": self.image,
            "EXPECTED_IMAGE_DIGEST": ROOT_DIGEST,
            "DESIRED_COUNT": "1",
            "API_DESIRED_COUNT": "1",
            "WORKER_DESIRED_COUNT": "0",
            "DEPLOYMENT_SMOKE_URL": "https://api.example.invalid/actuator/health",
        }
        self.service = {
            "serviceName": self.name,
            "status": "ACTIVE",
            "taskDefinition": self.td,
            "desiredCount": 1,
            "runningCount": 1,
            "pendingCount": 0,
            "deployments": [
                {
                    "status": "PRIMARY",
                    "taskDefinition": self.td,
                    "rolloutState": "COMPLETED",
                }
            ],
        }
        self.definition = {
            "taskDefinitionArn": self.td,
            "containerDefinitions": [
                {
                    "name": "server",
                    "image": self.image,
                    "healthCheck": {
                        "command": [
                            "CMD-SHELL",
                            "curl http://127.0.0.1:8080/actuator/health",
                        ]
                    },
                }
            ],
        }
        self.task = {
            "taskDefinitionArn": self.td,
            "lastStatus": "RUNNING",
            "healthStatus": "HEALTHY",
            "containers": [
                {
                    "name": "server",
                    "imageDigest": LEAF,
                    "lastStatus": "RUNNING",
                    "healthStatus": "HEALTHY",
                }
            ],
        }
        self.tasks = [self.task]
        self.manifest = {
            "schemaVersion": 2,
            "manifests": [
                {"platform": {"os": "linux", "architecture": "arm64"}, "digest": LEAF},
                {"platform": {"os": "linux", "architecture": "amd64"}, "digest": OTHER},
                {
                    "platform": {"os": "unknown", "architecture": "unknown"},
                    "digest": "sha256:" + "f" * 64,
                },
            ],
        }
        self.worker = {
            "serviceName": "ipgo-dev-ai-worker",
            "desiredCount": 0,
            "runningCount": 0,
            "pendingCount": 0,
        }
        self.calls = []
        self.smoke = Mock(return_value={"status": "UP"})

    def read(self, service, op, **params):
        self.calls.append((service, op))
        if op == "get-caller-identity":
            return {"Account": ACCOUNT}
        if op == "batch-get-image":
            return {
                "images": [
                    {
                        "imageId": {"imageDigest": ROOT_DIGEST},
                        "imageManifest": json.dumps(self.manifest),
                    }
                ]
            }
        if op == "describe-services":
            return {"services": [self.worker if params["services"] == ["ipgo-dev-ai-worker"] else self.service]}
        if op == "describe-task-definition":
            return {"taskDefinition": self.definition}
        if op == "list-tasks":
            return {"taskArns": ["fixture-task"] if self.tasks else []}
        if op == "describe-tasks":
            return {"tasks": self.tasks}
        raise AssertionError("Unexpected AWS operation")

    def check(self):
        return M.post_check(self.env, "server", self.read, self.smoke)

    def test_candidate_platform_digest_and_application_health_pass(self):
        self.assertEqual(self.check()["external_http"], "passed")
        self.smoke.assert_called_once_with(self.env["DEPLOYMENT_SMOKE_URL"])
        self.assertTrue(set(self.calls) <= M.READ_OPERATIONS)

    def test_index_digest_reported_by_ecs_is_supported(self):
        self.task["containers"][0]["imageDigest"] = ROOT_DIGEST
        self.assertEqual(self.check()["external_http"], "passed")

    def test_wrong_platform_or_attestation_digest_rejects(self):
        for digest in [OTHER, "sha256:" + "f" * 64]:
            self.task["containers"][0]["imageDigest"] = digest
            with self.subTest(digest=digest), self.assertRaises(M.GateError):
                self.check()

    def test_single_image_manifest_is_supported(self):
        self.manifest = {"schemaVersion": 2, "config": {"digest": OTHER}}
        self.task["containers"][0]["imageDigest"] = ROOT_DIGEST
        self.assertEqual(self.check()["external_http"], "passed")

    def test_stable_old_revision_after_rollback_is_not_success(self):
        self.service["taskDefinition"] = self.td[:-2] + "29"
        with self.assertRaises(M.GateError):
            self.check()
        self.smoke.assert_not_called()

    def test_wrong_rollout_counts_or_multiple_deployments_rejects(self):
        original = copy.deepcopy(self.service)
        for change in [
            {"pendingCount": 1},
            {"runningCount": 0},
            {"desiredCount": 2},
            {"serviceName": "other"},
            {"deployments": []},
            {
                "deployments": [
                    {
                        "status": "PRIMARY",
                        "taskDefinition": self.td,
                        "rolloutState": "FAILED",
                    }
                ]
            },
        ]:
            self.service = {**original, **change}
            with self.subTest(change=change), self.assertRaises(M.GateError):
                self.check()

    def test_unknown_unhealthy_or_missing_health_check_rejects(self):
        for health in ["UNKNOWN", "UNHEALTHY", None]:
            self.task["containers"][0]["healthStatus"] = health
            with self.subTest(health=health), self.assertRaises(M.GateError):
                self.check()
        self.task["containers"][0]["healthStatus"] = "HEALTHY"
        self.definition["containerDefinitions"][0].pop("healthCheck")
        with self.assertRaises(M.GateError):
            self.check()

    def test_old_running_task_or_wrong_image_reference_rejects(self):
        self.task["taskDefinitionArn"] = self.td[:-2] + "29"
        with self.assertRaises(M.GateError):
            self.check()
        self.task["taskDefinitionArn"] = self.td
        self.definition["containerDefinitions"][0]["image"] = "unapproved-image"
        with self.assertRaises(M.GateError):
            self.check()

    def test_missing_task_inspection_does_not_pass(self):
        def read(service, op, **params):
            if op == "describe-tasks":
                return {"tasks": [], "failures": [{"reason": "MISSING"}]}
            return self.read(service, op, **params)

        with self.assertRaises(M.GateError):
            M.post_check(self.env, "server", read, self.smoke)

    def test_scale_to_zero_does_not_claim_live_application_health(self):
        self.env["DESIRED_COUNT"] = "0"
        self.service.update(desiredCount=0, runningCount=0)
        self.tasks = []
        result = self.check()
        self.assertEqual(result["external_http"], "not_run_scaled_down")
        self.assertFalse(result["api"]["application_health_checked"])
        self.smoke.assert_not_called()

    def test_bad_http_body_or_network_error_rejects(self):
        for body in [{"status": "DOWN"}, {"status": "ok"}, "login page", None]:
            self.smoke.return_value = body
            with self.subTest(body=body), self.assertRaises(M.GateError):
                self.check()
        self.smoke.side_effect = TimeoutError("fixture-sensitive-text")
        with self.assertRaises(TimeoutError):
            self.check()

    def test_read_preflight_fails_on_wrong_account_or_denied_task_read(self):
        self.assertEqual(M.preflight(self.env, "server", self.read), {"read_preflight": "passed"})
        self.env["AWS_ACCOUNT_ID"] = "999999999999"
        with self.assertRaises(M.GateError):
            M.preflight(self.env, "server", self.read)
        self.env["AWS_ACCOUNT_ID"] = ACCOUNT

        def read(service, op, **params):
            if op == "list-tasks":
                raise PermissionError("fixture-sensitive-text")
            return self.read(service, op, **params)

        with self.assertRaises(PermissionError):
            M.preflight(self.env, "server", read)

    def test_ai_agent_http_health_and_stopped_worker_are_verified(self):
        old = self.td
        self.name = "ipgo-dev-onebox-ai-api"
        self.td = old.replace("onebox-server", "onebox-ai-api")
        self.env.update(
            ECS_SERVICE_NAME=self.name,
            EXPECTED_TASK_DEFINITION=self.td,
            DEPLOYMENT_SMOKE_URL="",
        )
        self.image = self.image.replace("/server:", "/ai:")
        self.env["EXPECTED_IMAGE"] = self.image
        self.definition["containerDefinitions"][0]["image"] = self.image
        self.service.update(serviceName=self.name, taskDefinition=self.td)
        self.service["deployments"][0]["taskDefinition"] = self.td
        self.definition["taskDefinitionArn"] = self.td
        self.definition["containerDefinitions"][0].update(
            name="ai-api",
            healthCheck={"command": ["CMD-SHELL", "python -c http://127.0.0.1:8000/health"]},
        )
        self.task["taskDefinitionArn"] = self.td
        self.task["containers"][0]["name"] = "ai-api"
        result = M.post_check(self.env, "ai", self.read, self.smoke)
        self.assertEqual(result["external_http"], "not_exposed_agent_http_health_checked")
        self.smoke.assert_not_called()
        self.worker["runningCount"] = 1
        with self.assertRaises(M.GateError):
            M.post_check(self.env, "ai", self.read, self.smoke)

    def test_aws_mutations_are_rejected_before_any_process(self):
        with patch.object(M.subprocess, "run") as run:
            with self.assertRaises(M.GateError):
                M.aws_read("ecs", "update-service")
            run.assert_not_called()

    def test_legacy_api_and_worker_are_verified_against_separate_revisions(self):
        image = self.image.replace("/server:", "/ai:").removesuffix("-arm64")
        api_name, worker_name = "ipgo-dev-ai-api", "ipgo-dev-ai-worker"
        api_td = self.td.replace(self.name, api_name)
        worker_td = self.td.replace(self.name, worker_name)
        self.env.update(
            COMPUTE_MODE="legacy",
            ECS_SERVICE_NAME=api_name,
            EXPECTED_IMAGE=image,
            EXPECTED_TASK_DEFINITION=api_td,
            EXPECTED_WORKER_TASK_DEFINITION=worker_td,
            WORKER_DESIRED_COUNT="1",
            DEPLOYMENT_SMOKE_URL="",
        )
        services, definitions, tasks = {}, {}, {}
        for name, arn, container in [(api_name, api_td, "ai-api"), (worker_name, worker_td, "ai-worker")]:
            services[name] = {
                **self.service,
                "serviceName": name,
                "taskDefinition": arn,
                "deployments": [{"status": "PRIMARY", "taskDefinition": arn, "rolloutState": "COMPLETED"}],
            }
            app = {"name": container, "image": image}
            if name == api_name:
                app["healthCheck"] = {"command": ["CMD-SHELL", "http://127.0.0.1:8000/health"]}
            definitions[arn] = {"taskDefinitionArn": arn, "containerDefinitions": [app]}
            tasks[name] = {
                "taskDefinitionArn": arn,
                "lastStatus": "RUNNING",
                "healthStatus": "HEALTHY",
                "containers": [
                    {
                        "name": container,
                        "lastStatus": "RUNNING",
                        "imageDigest": OTHER,
                        "healthStatus": "HEALTHY" if name == api_name else "UNKNOWN",
                    }
                ],
            }

        def read(service, op, **params):
            if op == "describe-services":
                return {"services": [services[params["services"][0]]]}
            if op == "describe-task-definition":
                return {"taskDefinition": definitions[params["taskDefinition"]]}
            if op == "list-tasks":
                return {"taskArns": [params["serviceName"]]}
            if op == "describe-tasks":
                return {"tasks": [tasks[params["tasks"][0]]]}
            return self.read(service, op, **params)

        result = M.post_check(self.env, "ai", read, self.smoke)
        self.assertEqual(result["worker"]["replicas"], 1)
        self.assertFalse(result["worker"]["application_health_checked"])
        services[worker_name]["taskDefinition"] = api_td
        with self.assertRaises(M.GateError):
            M.post_check(self.env, "ai", read, self.smoke)

    def test_incomplete_or_ambiguous_platform_manifest_rejects(self):
        platform = self.manifest["manifests"][0]
        for manifests in [[], [platform] * 2]:
            self.manifest["manifests"] = manifests
            with self.assertRaises(M.GateError):
                self.check()

    def test_task_list_pagination_is_not_silently_accepted(self):
        with self.assertRaises(M.GateError):
            M.task_list(lambda *a, **k: {"taskArns": [], "nextToken": "fixture"}, self.name)

    def test_main_never_logs_raw_aws_exception_or_token(self):
        with (
            patch.object(M.sys, "argv", ["verify", "post", "--service", "server"]),
            patch.dict(M.os.environ, self.env, clear=True),
            patch.object(M, "aws_read", side_effect=RuntimeError("fixture-secret-token")),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            self.assertEqual(M.main(), 1)
        self.assertNotIn("fixture-secret-token", err.getvalue())
        self.assertIn("rejected", err.getvalue())


class WorkflowVerificationTests(unittest.TestCase):
    def test_database_trigger_patterns_cover_executed_scripts(self):
        workflow = ROOT / ".github/workflows/database.yml"
        if not workflow.exists():
            self.skipTest("Database workflow belongs to Server")
        text = workflow.read_text()
        patterns = re.findall(r'^      - "([^"]+)"$', text, re.MULTILINE)
        self.assertEqual(set(patterns), set(M.DB_PATHS))
        scripts = re.findall(r"(scripts/[\w/.-]+\.sh)", text)
        for script in scripts + ["scripts/testing/verify-monthly-insight-budget.sh"]:
            self.assertTrue(
                any(M.fnmatch.fnmatchcase(script, pattern) for pattern in patterns),
                script,
            )

    def test_ci_gate_precedes_aws_and_preflight_precedes_ecr_write(self):
        text = (ROOT / ".github/workflows/deploy-dev.yml").read_text()
        self.assertLess(
            text.index(" ci --service"),
            text.index("aws-actions/configure-aws-credentials@"),
        )
        self.assertLess(
            text.index(" preflight --service"),
            text.index("Build and push immutable image"),
        )
        self.assertLess(text.index("Set approved task count"), text.index(" post --service"))
        self.assertIn('git show "$WORKFLOW_SHA:scripts/deployment/verify_deployment.py"', text)
        self.assertIn(
            "EXPECTED_TASK_DEFINITION: ${{ steps.deploy-app.outputs.task-definition-arn }}",
            text,
        )
        self.assertIn("EXPECTED_IMAGE_DIGEST: ${{ steps.verified_image.outputs.digest }}", text)

    def test_smoke_url_and_redirect_policy(self):
        M.validate_url("https://api.example.invalid/health")
        for url in [
            "http://api.example.invalid/health",
            "https://user:pass@api.example.invalid/health",
            "https://api.example.invalid/health?token=x",
            "https://api.example.invalid:8443/health",
            "",
        ]:
            with self.subTest(url=url), self.assertRaises(M.GateError):
                M.validate_url(url)
        with self.assertRaises(M.GateError):
            M.NoRedirect().redirect_request(None, None, 302, "", {}, "https://login.example.invalid")


class ImageSourceCompatibilityTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("ecr_source", ROOT / "scripts/deployment/check_ecr_image.py")
        self.ecr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ecr)
        self.env = {
            "AWS_ACCOUNT_ID": ACCOUNT,
            "ECR_REGISTRY": f"{ACCOUNT}.dkr.ecr.ap-northeast-2.amazonaws.com",
            "ECR_REPOSITORY": "ipgo-dev/server",
            "GITHUB_SHA": HEAD,
            "DEPLOYMENT_SOURCE_SHA": SHA,
            "EXPECTED_SHA": SHA,
            "COMPUTE_MODE": "onebox",
            "IMAGE_TAG": SHA + "-arm64",
        }

    def test_approved_old_source_not_new_workflow_sha_drives_image(self):
        self.assertEqual(self.ecr.identity(self.env)[3], SHA + "-arm64")

    def test_invalid_or_unapproved_source_never_uses_workflow_fallback(self):
        for change in [{"DEPLOYMENT_SOURCE_SHA": "invalid"}, {"EXPECTED_SHA": HEAD}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.ecr.identity({**self.env, **change})

    def test_legacy_caller_without_source_still_requires_matching_sha_tag(self):
        self.env.pop("DEPLOYMENT_SOURCE_SHA")
        self.env.update(GITHUB_SHA=SHA)
        self.assertEqual(self.ecr.identity(self.env)[3], SHA + "-arm64")


class HttpVerificationTests(unittest.TestCase):
    def test_http_requires_200_json_without_following_redirects(self):
        with patch.object(M, "build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{"status":"UP"}'
            self.assertEqual(M.http_json("https://api.example.invalid/health"), {"status": "UP"})
            response.status = 302
            with self.assertRaises(M.GateError):
                M.http_json("https://api.example.invalid/health")
            response.status = 200
            response.read.return_value = b"<html>login</html>"
            with self.assertRaises(ValueError):
                M.http_json("https://api.example.invalid/health")


if __name__ == "__main__":
    unittest.main()
