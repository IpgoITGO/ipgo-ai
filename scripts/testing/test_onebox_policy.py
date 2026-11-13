"""Infra #61 policy failures stop AI deployment; no AWS/network writes are used."""

import contextlib
import copy
import io
import unittest
from unittest.mock import patch

import test_deployment_verification as existing

M = existing.M
ROOT = existing.ROOT
ACCOUNT = existing.ACCOUNT


class OneboxPolicyTests(unittest.TestCase):
    def setUp(self):
        self.name = "ipgo-dev-onebox-ai-api"
        self.arn = f"arn:aws:ecs:{M.REGION}:{ACCOUNT}:task-definition/{self.name}:21"
        self.env = {"AWS_ACCOUNT_ID": ACCOUNT, "COMPUTE_MODE": "onebox", "ECS_SERVICE_NAME": self.name}
        self.current = {
            "serviceName": self.name, "status": "ACTIVE", "taskDefinition": self.arn,
            "launchType": "EC2", "deploymentController": {"type": "ECS"},
            "deploymentConfiguration": {
                "minimumHealthyPercent": 0, "maximumPercent": 100, "strategy": "ROLLING",
                "deploymentCircuitBreaker": {"enable": True, "rollback": True},
            },
            "healthCheckGracePeriodSeconds": 300, "desiredCount": 1, "runningCount": 1, "pendingCount": 0,
        }
        self.definition = {
            "taskDefinitionArn": self.arn, "family": self.name, "networkMode": "bridge", "memory": "256",
            "requiresCompatibilities": ["EC2"],
            "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
            "containerDefinitions": [{
                "name": "ai-api", "memory": 256,
                "portMappings": [{"containerPort": 8000, "hostPort": 18000, "protocol": "tcp", "name": "http"}],
            }],
        }
        self.worker = {
            "serviceName": "ipgo-dev-ai-worker", "status": "ACTIVE",
            "desiredCount": 0, "runningCount": 0, "pendingCount": 0,
        }
        self.worker_response = {"services": [self.worker], "failures": []}
        self.calls = []

    def read(self, service, operation, **params):
        self.calls.append((service, operation))
        if (service, operation) == ("sts", "get-caller-identity"):
            return {"Account": ACCOUNT}
        self.assertEqual(service, "ecs")
        if operation == "describe-services":
            self.assertEqual(params["cluster"], M.CLUSTER)
            if params["services"] == ["ipgo-dev-ai-worker"]:
                return copy.deepcopy(self.worker_response)
            self.assertEqual(params["services"], [self.env["ECS_SERVICE_NAME"]])
            return {"services": [copy.deepcopy(self.current)], "failures": []}
        if operation == "describe-task-definition":
            self.assertEqual(params["taskDefinition"], self.arn)
            return {"taskDefinition": copy.deepcopy(self.definition)}
        if operation == "list-tasks":
            return {"taskArns": []}
        self.fail("Unexpected read or mutation")

    def check(self):
        return M.preflight(self.env, "ai", self.read)

    def test_matching_policy_and_approved_scaled_down_state(self):
        self.assertEqual(self.check(), {"read_preflight": "passed"})
        self.current.update(desiredCount=0, runningCount=0)
        self.assertEqual(self.check(), {"read_preflight": "passed"})
        self.assertTrue(all(call in M.READ_OPERATIONS for call in self.calls))

    def test_each_required_policy_field_rejects_drift_and_missing_values(self):
        original = copy.deepcopy(self.current)
        for parent, key, wrong in [
            ((), "launchType", "FARGATE"), ((), "healthCheckGracePeriodSeconds", 0),
            (("deploymentController",), "type", "CODE_DEPLOY"),
            (("deploymentConfiguration",), "minimumHealthyPercent", 100),
            (("deploymentConfiguration",), "maximumPercent", 200),
            (("deploymentConfiguration",), "strategy", "BLUE_GREEN"),
            (("deploymentConfiguration", "deploymentCircuitBreaker"), "enable", False),
            (("deploymentConfiguration", "deploymentCircuitBreaker"), "rollback", False),
        ]:
            for missing in (False, True):
                self.current = copy.deepcopy(original)
                target = self.current
                for part in parent:
                    target = target[part]
                if missing:
                    del target[key]
                else:
                    target[key] = wrong
                with self.subTest(key=key, missing=missing), self.assertRaises(M.GateError):
                    self.check()

    def test_task_definition_constraints_and_response_identity(self):
        original = copy.deepcopy(self.definition)
        for parent, key, wrong in [
            ((), "taskDefinitionArn", self.arn + "0"), ((), "family", "other"),
            ((), "networkMode", "awsvpc"), ((), "memory", "512"),
            ((), "requiresCompatibilities", ["FARGATE"]),
            (("runtimePlatform",), "cpuArchitecture", "X86_64"),
            (("runtimePlatform",), "operatingSystemFamily", "WINDOWS_SERVER_2022_CORE"),
            (("containerDefinitions", 0), "memory", 512),
            (("containerDefinitions", 0, "portMappings", 0), "hostPort", 0),
            (("containerDefinitions", 0, "portMappings", 0), "containerPort", 9000),
            (("containerDefinitions", 0, "portMappings", 0), "protocol", "udp"),
        ]:
            for missing in (False, True):
                self.definition = copy.deepcopy(original)
                target = self.definition
                for part in parent:
                    target = target[part]
                if missing:
                    del target[key]
                else:
                    target[key] = wrong
                with self.subTest(key=key, missing=missing), self.assertRaises(M.GateError):
                    self.check()

    def test_missing_containers_ports_and_invalid_counts_fail_closed(self):
        original = copy.deepcopy(self.definition)
        for containers in ([], [{"name": "ai-api", "memory": 256}], original["containerDefinitions"] * 2):
            self.definition = {**original, "containerDefinitions": containers}
            with self.subTest(containers=containers), self.assertRaises(M.GateError):
                self.check()
        self.definition = original
        for key in ("desiredCount", "runningCount", "pendingCount"):
            for value in (None, -1, 2, True, "0"):
                saved = self.current[key]
                self.current[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(M.GateError):
                    self.check()
                self.current[key] = saved
        self.current["pendingCount"] = 1
        with self.assertRaises(M.GateError):
            self.check()

    def test_separate_worker_must_exist_and_be_fully_stopped(self):
        original = copy.deepcopy(self.worker_response)
        for key in ("desiredCount", "runningCount", "pendingCount", "serviceName", "status"):
            for missing in (False, True):
                self.worker_response = copy.deepcopy(original)
                worker = self.worker_response["services"][0]
                if missing:
                    del worker[key]
                else:
                    worker[key] = 1 if key.endswith("Count") else "unexpected"
                with self.subTest(key=key, missing=missing), self.assertRaises(M.GateError):
                    self.check()
        for response in ({"services": [], "failures": []}, {**original, "failures": [{"reason": "MISSING"}]}):
            self.worker_response = response
            with self.subTest(response=response), self.assertRaises(M.GateError):
                self.check()

    def test_policy_is_read_again_and_drift_is_never_repaired(self):
        self.check()
        self.current["deploymentConfiguration"]["maximumPercent"] = 200
        with self.assertRaises(M.GateError):
            self.check()
        self.assertEqual(self.current["deploymentConfiguration"]["maximumPercent"], 200)
        self.assertTrue(all(call in M.READ_OPERATIONS for call in self.calls))

    def test_wrong_account_and_service_are_rejected(self):
        self.env["AWS_ACCOUNT_ID"] = "999999999999"
        with self.assertRaises(M.GateError):
            self.check()
        self.env["AWS_ACCOUNT_ID"] = ACCOUNT
        self.current["serviceName"] = "other"
        with self.assertRaises(M.GateError):
            self.check()

    def test_read_errors_reject_without_logging_raw_responses(self):
        for failed in ("get-caller-identity", "describe-services", "describe-task-definition", "list-tasks"):
            original = self.read

            def read(service, operation, failed_operation=failed, original_read=original, **params):
                if operation == failed_operation:
                    raise PermissionError("fixture-sensitive-value")
                return original_read(service, operation, **params)

            with (
                self.subTest(operation=failed), patch.dict(M.os.environ, self.env, clear=True),
                patch.object(M.sys, "argv", ["verify", "preflight", "--service", "ai"]),
                patch.object(M, "aws_read", read), contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                self.assertEqual(M.main(), 1)
                self.assertNotIn("fixture-sensitive-value", err.getvalue())

    def test_legacy_and_server_paths_do_not_require_onebox_ai_policy(self):
        for mode, service, name in [
            ("legacy", "ai", "ipgo-dev-ai-api"), ("", "ai", "ipgo-dev-ai-api"),
            ("onebox", "server", "ipgo-dev-onebox-server"),
        ]:
            self.calls = []
            self.env.update(COMPUTE_MODE=mode, ECS_SERVICE_NAME=name)
            self.current = {"serviceName": name, "status": "ACTIVE"}
            with self.subTest(mode=mode, service=service):
                self.assertEqual(M.preflight(self.env, service, self.read), {"read_preflight": "passed"})
                self.assertNotIn(("ecs", "describe-task-definition"), self.calls)


class PolicyWorkflowTests(unittest.TestCase):
    def test_both_policy_guards_run_before_the_corresponding_writes(self):
        text = (ROOT / ".github/workflows/deploy-dev.yml").read_text()
        self.assertNotIn("--deployment-configuration", text)
        self.assertNotIn("Configure approved one-box restart", text)
        guards = [i for i in range(len(text)) if text.startswith(" preflight --service ai", i)]
        self.assertEqual(len(guards), 2)
        self.assertLess(guards[0], text.index("Build and push immutable image"))
        template = text.index(" check-template --service ai")
        deploy = text.index("      - name: Deploy API task definition")
        self.assertLess(template, guards[1])
        self.assertLess(guards[1], deploy)
        self.assertIn("Set approved task counts", text)
        for approval in ("maintenance_approved", "real_capacity_approved", "llm_approved", "expected_sha"):
            self.assertIn(approval, text)
        self.assertLess(deploy, text.index(" wait-rollout --service ai"))
        self.assertIn(" post --service ai", text)


if __name__ == "__main__":
    unittest.main()
