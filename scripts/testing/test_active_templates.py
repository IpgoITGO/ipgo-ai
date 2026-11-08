"""Active task templates and real Git rename checks, without network or AWS writes."""

import contextlib
import copy
import importlib.util
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("active_verification", ROOT / "scripts/deployment/verify_deployment.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
ACCOUNT = "123456789012"


class GitRenameTests(unittest.TestCase):
    def test_git_diff_preserves_db_paths_on_rename_in_out_and_deletion(self):
        with tempfile.TemporaryDirectory() as directory:

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=directory, text=True).strip()

            git("init", "-q")
            git("config", "diff.renames", "true")
            git("config", "commit.gpgsign", "false")
            git("config", "user.name", "Offline Fixture")
            git("config", "user.email", "fixture@example.invalid")
            old, new = "database/fixture.sql", "docs/archived.sql"
            path = Path(directory) / old
            path.parent.mkdir(parents=True)
            path.write_text("-- synthetic fixture\nSELECT 1;\n")
            git("add", ".")
            git("commit", "-qm", "fixture base")
            (Path(directory) / new).parent.mkdir()
            git("mv", old, new)
            git("commit", "-qm", "fixture move out")
            sha = git("rev-parse", "HEAD")
            # Reproduce the old missing-path condition before testing the fix.
            self.assertEqual(git("diff", "HEAD^1", "HEAD", "--name-only"), new)
            self.assertEqual(set(M.changed_paths(sha, directory)), {old, new})
            self.assertTrue(M.database_required(M.changed_paths(sha, directory)))
            git("mv", new, old)
            git("commit", "-qm", "fixture move in")
            self.assertEqual(set(M.changed_paths(git("rev-parse", "HEAD"), directory)), {old, new})
            git("rm", old)
            git("commit", "-qm", "fixture removal")
            self.assertEqual(M.changed_paths(git("rev-parse", "HEAD"), directory), [old])

    def test_invalid_sha_rejects_before_git(self):
        with patch.object(M.subprocess, "run") as run:
            with self.assertRaises(M.GateError):
                M.changed_paths("HEAD;unexpected")
            run.assert_not_called()


class ActiveTemplateTests(unittest.TestCase):
    def setUp(self):
        self.env = {"AWS_ACCOUNT_ID": ACCOUNT, "COMPUTE_MODE": "onebox", "ECS_SERVICE_NAME": "ipgo-dev-onebox-server"}
        self.services, self.definitions, self.calls = {}, {}, []
        for name, revision in [
            ("ipgo-dev-onebox-server", 7),
            ("ipgo-dev-onebox-ai-api", 8),
            ("ipgo-dev-server", 10),
            ("ipgo-dev-ai-api", 11),
            ("ipgo-dev-ai-worker", 12),
        ]:
            arn = f"arn:aws:ecs:ap-northeast-2:{ACCOUNT}:task-definition/{name}:{revision}"
            self.services[name] = {
                "status": "ACTIVE",
                "serviceName": name,
                "taskDefinition": arn,
                "deployments": [{"status": "PRIMARY", "taskDefinition": arn, "rolloutState": "COMPLETED"}],
            }
            self.definitions[arn] = {
                "taskDefinitionArn": arn,
                "family": name,
                "containerDefinitions": [
                    {"name": "fixture", "environment": [{"name": "FIXTURE", "value": "active-value"}]}
                ],
            }

    def read(self, service, operation, **params):
        self.calls.append((service, operation, params))
        self.assertEqual(service, "ecs")
        if operation == "describe-services":
            self.assertEqual(params["cluster"], M.CLUSTER)
            return {"services": [copy.deepcopy(self.services[n]) for n in params["services"]], "failures": []}
        if operation == "describe-task-definition":
            # A family lookup would select an unrelated latest revision; it is forbidden.
            self.assertIn(params["taskDefinition"], self.definitions)
            return {"taskDefinition": copy.deepcopy(self.definitions[params["taskDefinition"]])}
        self.fail("Unsupported AWS operation")

    def test_active_revision_is_used_instead_of_orphan_latest(self):
        name = self.env["ECS_SERVICE_NAME"]
        latest = f"arn:aws:ecs:ap-northeast-2:{ACCOUNT}:task-definition/{name}:99"
        self.definitions[latest] = {"taskDefinitionArn": latest, "family": name, "fixture": "orphan-latest"}
        templates, snapshot = M.capture_templates(self.env, "server", self.read)
        arn = self.services[name]["taskDefinition"]
        self.assertEqual(snapshot, {name: arn})
        self.assertEqual(templates["task-definition.json"], self.definitions[arn])
        self.assertEqual([c[2]["taskDefinition"] for c in self.calls if c[1] == "describe-task-definition"], [arn])
        self.assertEqual(
            M.check_templates(self.env, "server", self.read, snapshot), {"active_templates_unchanged": True}
        )

    def test_legacy_ai_api_and_worker_keep_their_own_active_revision(self):
        self.env.update(COMPUTE_MODE="legacy", ECS_SERVICE_NAME="ipgo-dev-ai-api")
        templates, snapshot = M.capture_templates(self.env, "ai", self.read)
        self.assertEqual(set(templates), {"ai-api-task-definition.json", "ai-worker-task-definition.json"})
        self.assertEqual(set(snapshot), {"ipgo-dev-ai-api", "ipgo-dev-ai-worker"})
        self.assertNotEqual(snapshot["ipgo-dev-ai-api"], snapshot["ipgo-dev-ai-worker"])
        self.assertTrue(M.check_templates(self.env, "ai", self.read, snapshot))
        self.services["ipgo-dev-ai-worker"]["taskDefinition"] += "0"
        self.services["ipgo-dev-ai-worker"]["deployments"][0]["taskDefinition"] += "0"
        with self.assertRaises(M.GateError):
            M.check_templates(self.env, "ai", self.read, snapshot)

    def test_onebox_ai_does_not_read_separate_worker_template(self):
        self.env["ECS_SERVICE_NAME"] = "ipgo-dev-onebox-ai-api"
        templates, snapshot = M.capture_templates(self.env, "ai", self.read)
        self.assertEqual(set(templates), {"ai-api-task-definition.json"})
        self.assertEqual(set(snapshot), {"ipgo-dev-onebox-ai-api"})

    def test_legacy_server_selects_legacy_service(self):
        self.env.update(COMPUTE_MODE="legacy", ECS_SERVICE_NAME="ipgo-dev-server")
        _, snapshot = M.capture_templates(self.env, "server", self.read)
        self.assertEqual(set(snapshot), {"ipgo-dev-server"})

    def test_wrong_account_region_family_or_missing_revision_rejects(self):
        current = self.services[self.env["ECS_SERVICE_NAME"]]
        original = current["taskDefinition"]
        for arn in [
            original.replace(ACCOUNT, "999999999999"),
            original.replace("ap-northeast-2", "us-east-1"),
            original.replace("onebox-server", "onebox-ai-api"),
            original.rsplit(":", 1)[0],
            None,
        ]:
            current["taskDefinition"] = arn
            with self.subTest(arn=arn), self.assertRaises(M.GateError):
                M.capture_templates(self.env, "server", self.read)

    def test_missing_or_unexpected_service_rejects(self):
        for response in [
            {"services": [], "failures": [{"reason": "MISSING"}]},
            {"services": [{"status": "INACTIVE", "serviceName": self.env["ECS_SERVICE_NAME"]}]},
            {"services": [{"status": "ACTIVE", "serviceName": "wrong-service"}]},
        ]:
            with self.subTest(response=response), self.assertRaises(M.GateError):
                M.capture_templates(self.env, "server", lambda *a, response=response, **kw: response)

    def test_incomplete_or_concurrent_rollout_rejects(self):
        current = self.services[self.env["ECS_SERVICE_NAME"]]
        original = copy.deepcopy(current["deployments"])
        for deployments in [
            [],
            original * 2,
            [{**original[0], "rolloutState": "IN_PROGRESS"}],
            [{**original[0], "status": "ACTIVE"}],
            [{**original[0], "taskDefinition": "other"}],
        ]:
            current["deployments"] = deployments
            with self.subTest(deployments=deployments), self.assertRaises(M.GateError):
                M.capture_templates(self.env, "server", self.read)

    def test_describe_task_response_must_match_exact_snapshot(self):
        arn = self.services[self.env["ECS_SERVICE_NAME"]]["taskDefinition"]
        original = copy.deepcopy(self.definitions[arn])
        for field, value in [("taskDefinitionArn", arn + "0"), ("family", "other")]:
            self.definitions[arn] = {**original, field: value}
            with self.subTest(field=field), self.assertRaises(M.GateError):
                M.capture_templates(self.env, "server", self.read)

    def test_changed_revision_or_snapshot_target_rejects(self):
        _, snapshot = M.capture_templates(self.env, "server", self.read)
        for changed in [{}, {"unexpected": "value"}, {**snapshot, "unexpected": "value"}]:
            with self.subTest(snapshot=changed), self.assertRaises(M.GateError):
                M.check_templates(self.env, "server", self.read, changed)
        current = self.services[self.env["ECS_SERVICE_NAME"]]
        current["taskDefinition"] += "0"
        current["deployments"][0]["taskDefinition"] = current["taskDefinition"]
        with self.assertRaises(M.GateError):
            M.check_templates(self.env, "server", self.read, snapshot)

    def test_cli_saves_private_templates_and_checks_them_without_logging_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**self.env, "RUNNER_TEMP": directory}
            with patch.dict(M.os.environ, env, clear=True), patch.object(M, "aws_read", self.read):
                for command in ["snapshot", "check-template"]:
                    with (
                        patch.object(M.sys, "argv", ["verify", command, "--service", "server"]),
                        contextlib.redirect_stdout(io.StringIO()) as output,
                    ):
                        self.assertEqual(M.main(), 0)
                    self.assertNotIn("active-value", output.getvalue())
                    self.assertNotIn(ACCOUNT, output.getvalue())
                for name in ["task-definition.json", "source-task-definitions.json"]:
                    path = Path(directory) / name
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertTrue(json.loads(path.read_text()))
                # Even an existing permissive file is made private on the next capture.
                os.chmod(Path(directory) / "task-definition.json", 0o644)
                with (
                    patch.object(M.sys, "argv", ["verify", "snapshot", "--service", "server"]),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(M.main(), 0)
                self.assertEqual(stat.S_IMODE((Path(directory) / "task-definition.json").stat().st_mode), 0o600)

    def test_cli_missing_snapshot_and_aws_errors_fail_without_raw_payload(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(M.os.environ, {**self.env, "RUNNER_TEMP": directory}, clear=True),
        ):
            for command in ["check-template", "snapshot"]:
                with (
                    patch.object(M.sys, "argv", ["verify", command, "--service", "server"]),
                    patch.object(M, "aws_read", side_effect=RuntimeError("fixture-sensitive-value")),
                    contextlib.redirect_stderr(io.StringIO()) as output,
                ):
                    self.assertEqual(M.main(), 1)
                self.assertNotIn("fixture-sensitive-value", output.getvalue())

    def test_workflow_uses_snapshot_and_guard_before_ecs_writes(self):
        workflow = (ROOT / ".github/workflows/deploy-dev.yml").read_text()
        self.assertNotIn("ECS_TASK_FAMILY", workflow)
        self.assertNotIn("aws ecs describe-task-definition", workflow)
        snapshot = workflow.index(" snapshot --service")
        guard = workflow.index(" check-template --service")
        first_write = workflow.index("aws ecs update-service")
        self.assertLess(snapshot, guard)
        self.assertLess(guard, first_write)
        self.assertIn('python3 "$RUNNER_TEMP/verify_deployment.py" snapshot', workflow)


if __name__ == "__main__":
    unittest.main()
