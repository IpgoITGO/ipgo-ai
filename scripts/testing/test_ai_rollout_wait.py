"""AI action의 조기 반환 이후 완료 대기/엄격 post 연결을 오프라인 검증한다."""
import contextlib
import copy
import io
import re
import unittest
from unittest.mock import Mock, patch

import test_deployment_verification as existing

M = existing.M
ROOT = existing.ROOT


class AiRolloutWaitTests(unittest.TestCase):
    def setUp(self):
        fixture = existing.RuntimeVerificationTests()
        fixture.setUp()
        old = fixture.td
        fixture.name = "ipgo-dev-onebox-ai-api"
        fixture.td = old.replace("onebox-server", "onebox-ai-api")
        fixture.env.update(
            ECS_SERVICE_NAME=fixture.name,
            EXPECTED_TASK_DEFINITION=fixture.td,
            DEPLOYMENT_SMOKE_URL="",
        )
        fixture.image = fixture.image.replace("/server:", "/ai:")
        fixture.env["EXPECTED_IMAGE"] = fixture.image
        fixture.service.update(serviceName=fixture.name, taskDefinition=fixture.td)
        fixture.service["deployments"][0]["taskDefinition"] = fixture.td
        fixture.definition["taskDefinitionArn"] = fixture.td
        fixture.definition["containerDefinitions"][0].update(
            name="ai-api", image=fixture.image,
            healthCheck={"command": ["CMD-SHELL", "python -c http://127.0.0.1:8000/health"]},
        )
        fixture.task["taskDefinitionArn"] = fixture.td
        fixture.task["containers"][0]["name"] = "ai-api"
        self.fixture = fixture
        self.env = fixture.env
        self.ready = fixture.service
        self.now = 0
        self.delay = 0
        self.calls = []
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def wait(self, states):
        queue = list(states)
        last = states[-1]

        def read(service, operation, **params):
            self.calls.append((service, operation))
            self.assertEqual((service, operation), ("ecs", "describe-services"))
            self.assertEqual(params, {"cluster": M.CLUSTER, "services": [self.env["ECS_SERVICE_NAME"]]})
            self.now += self.delay
            state = queue.pop(0) if queue else last
            if isinstance(state, Exception):
                raise state
            return copy.deepcopy(state if "services" in state else {"services": [state]})

        clock = Mock(monotonic=lambda: self.now, sleep=self.sleep)
        with patch.object(M, "time", clock):
            return M.wait_rollout(self.env, "ai", read)

    def pending(self):
        state = copy.deepcopy(self.ready)
        state["deployments"][0]["rolloutState"] = "IN_PROGRESS"
        return state

    def test_completed_candidate_never_sleeps(self):
        self.assertEqual(self.wait([self.ready]), {"candidate_rollout_completed": True, "observations": 1})
        self.assertEqual(self.sleeps, [])

    def test_waits_for_rollout_and_replica_counts(self):
        pending = self.pending()
        counts = copy.deepcopy(self.ready)
        counts.update(runningCount=0, pendingCount=1)
        self.assertEqual(self.wait([pending, counts, self.ready])["observations"], 3)
        self.assertEqual(self.sleeps, [5, 5])

    def test_completed_primary_waits_for_previous_deployment_cleanup(self):
        cleaning = copy.deepcopy(self.ready)
        previous = copy.deepcopy(cleaning["deployments"][0])
        previous.update(status="ACTIVE", taskDefinition=previous["taskDefinition"].rsplit(":", 1)[0] + ":29")
        cleaning["deployments"].append(previous)
        self.assertEqual(self.wait([cleaning, self.ready])["observations"], 2)

    def test_rollback_during_wait_is_immediately_fatal(self):
        old = copy.deepcopy(self.ready)
        old["taskDefinition"] = old["taskDefinition"].rsplit(":", 1)[0] + ":29"
        with self.assertRaisesRegex(M.GateError, "possible rollback"):
            self.wait([self.pending(), old, self.ready])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [5])

    def test_failed_missing_or_unknown_primary_state_is_not_retried(self):
        for value in ["FAILED", None, "UNRECOGNIZED"]:
            with self.subTest(state=value):
                self.calls.clear()
                state = copy.deepcopy(self.ready)
                state["deployments"][0]["rolloutState"] = value
                with self.assertRaises(M.GateError):
                    self.wait([state, self.ready])
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.sleeps, [])

    def test_primary_identity_and_evidence_fail_without_retry(self):
        cases = []
        for field, value in [("desiredCount", 2), ("serviceName", "wrong"), ("status", "DRAINING")]:
            state = copy.deepcopy(self.ready)
            state[field] = value
            cases.append(state)
        missing = copy.deepcopy(self.ready)
        missing["deployments"] = []
        duplicate = copy.deepcopy(self.ready)
        duplicate["deployments"] *= 2
        wrong = copy.deepcopy(self.ready)
        wrong["deployments"][0]["taskDefinition"] += "0"
        foreign = copy.deepcopy(self.ready)
        foreign["deployments"].append(
            {"status": "ACTIVE", "taskDefinition": "arn:aws:ecs:wrong:000000000000:task-definition/other:1"}
        )
        cases += [missing, duplicate, wrong, foreign, {"services": [], "failures": [{"reason": "MISSING"}]}]
        for state in cases:
            with self.subTest(state=state):
                self.calls.clear()
                with self.assertRaises(M.GateError):
                    self.wait([state, self.ready])
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.sleeps, [])

    def test_invalid_expected_arn_fails_before_any_aws_read(self):
        self.env["EXPECTED_TASK_DEFINITION"] = "unapproved"
        with self.assertRaises(M.GateError):
            self.wait([self.ready])
        self.assertEqual(self.calls, [])

    def test_aws_error_is_not_retried(self):
        errors = [
            OSError("synthetic connection failure"),
            M.subprocess.CalledProcessError(254, ["aws"], stderr="synthetic AccessDenied"),
            M.subprocess.TimeoutExpired(["aws"], 30),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.calls.clear()
                with self.assertRaises(type(error)):
                    self.wait([error, self.ready])
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.sleeps, [])

    def test_timeout_is_bounded_without_real_sleep(self):
        with self.assertRaisesRegex(M.GateError, "timed out"):
            self.wait([self.pending()])
        self.assertEqual(self.now, 60)
        self.assertLessEqual(len(self.calls), 13)
        self.assertTrue(all(seconds <= 5 for seconds in self.sleeps))

    def test_completed_response_after_deadline_cannot_pass(self):
        self.delay = 61
        with self.assertRaisesRegex(M.GateError, "timed out"):
            self.wait([self.ready])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_cli_does_not_echo_aws_error_payload(self):
        with (
            patch.dict(M.os.environ, self.env, clear=True),
            patch.object(M, "aws_read", side_effect=OSError("synthetic denied payload")) as read,
            patch.object(M.sys, "argv", ["verify", "wait-rollout", "--service", "ai"]),
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(M.main(), 1)
        self.assertEqual(read.call_count, 1)
        self.assertNotIn("synthetic denied payload", error.getvalue())

    def test_workflow_waits_for_transient_candidate_before_strict_post(self):
        text = (ROOT / ".github/workflows/deploy-dev.yml").read_text()
        tail = text.split("Set approved task counts", 1)[1]
        commands = re.findall(r'verify_deployment\.py" (wait-rollout|post) --service ai', tail)
        pending = self.pending()  # ServicesStable가 통과할 수 있는 수량과 단일 deployment.

        def read(service, operation, **params):
            if operation == "describe-services" and params["services"] == [self.env["ECS_SERVICE_NAME"]]:
                self.calls.append((service, operation))
                return {"services": [pending if self.now < 10 else self.ready]}
            return self.fixture.read(service, operation, **params)

        clock = Mock(monotonic=lambda: self.now, sleep=self.sleep)
        with (
            patch.dict(M.os.environ, self.env, clear=True),
            patch.object(M, "aws_read", read),
            patch.object(M, "http_json", self.fixture.smoke),
            patch.object(M, "time", clock, create=True),
        ):
            for command in commands:
                with (
                    patch.object(M.sys, "argv", ["verify", command, "--service", "ai"]),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()) as error,
                ):
                    self.assertEqual(M.main(), 0, error.getvalue())
        self.assertEqual(commands, ["wait-rollout", "post"])
        self.assertEqual(self.sleeps, [5, 5])
        self.fixture.smoke.assert_not_called()
        self.assertTrue(set(self.calls + self.fixture.calls) <= M.READ_OPERATIONS)

    def test_workflow_keeps_candidate_output_deadline_and_fail_closed_step(self):
        text = (ROOT / ".github/workflows/deploy-dev.yml").read_text()
        block = text.split("- name: Wait for exact API candidate rollout", 1)[1].split("- name:", 1)[0]
        self.assertIn("timeout-minutes: 2", block)
        self.assertIn("EXPECTED_TASK_DEFINITION: ${{ steps.deploy-app.outputs.task-definition-arn }}", block)
        self.assertIn("API_DESIRED_COUNT: ${{ inputs.api_desired_count }}", block)
        self.assertNotIn("continue-on-error", block)
        self.assertNotIn("always()", block)
        self.assertIn("wait-max-delay-seconds: 15", text)

    def test_api_count_is_used_and_scale_to_zero_is_supported(self):
        self.env["DESIRED_COUNT"] = "2"
        self.assertTrue(self.wait([self.ready])["candidate_rollout_completed"])
        self.env["API_DESIRED_COUNT"] = "0"
        zero = copy.deepcopy(self.ready)
        zero.update(desiredCount=0, runningCount=0, pendingCount=0)
        self.assertTrue(self.wait([zero])["candidate_rollout_completed"])
