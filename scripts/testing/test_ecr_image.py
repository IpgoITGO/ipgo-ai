import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deployment/check_ecr_image.py"
SPEC = importlib.util.spec_from_file_location("check_ecr_image", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
ACCOUNT = "123456789012"


class EcrImageTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "AWS_ACCOUNT_ID": ACCOUNT,
            "ECR_REGISTRY": f"{ACCOUNT}.dkr.ecr.ap-northeast-2.amazonaws.com",
            "ECR_REPOSITORY": "ipgo-dev/server",
            "GITHUB_SHA": SHA,
            "COMPUTE_MODE": "onebox",
            "IMAGE_TAG": SHA + "-arm64",
        }
        self.present = {
            "images": [{
                "registryId": ACCOUNT,
                "repositoryName": "ipgo-dev/server",
                "imageId": {"imageTag": SHA + "-arm64", "imageDigest": DIGEST},
                "imageManifestMediaType": "application/vnd.oci.image.index.v1+json",
                "imageManifest": '{"schemaVersion":2,"manifests":[]}',
            }],
            "failures": [],
        }
        self.missing = {
            "images": [],
            "failures": [{"failureCode": "ImageNotFound", "imageId": {"imageTag": SHA + "-arm64"}}],
        }

    def resolve(self, response, **kwargs):
        return MODULE.resolve(self.env, lookup=lambda *_: response, **kwargs)

    def test_existing_sha_reuses_without_build(self):
        result = self.resolve(self.present)
        self.assertEqual(result["exists"], "true")
        self.assertEqual(result["digest"], DIGEST)
        self.assertTrue(result["image"].endswith("/ipgo-dev/server:" + SHA + "-arm64"))

    def test_missing_sha_is_the_only_build_path(self):
        result = self.resolve(self.missing)
        self.assertEqual(result["exists"], "false")
        self.assertEqual(result["digest"], "")

    def test_ai_repository_uses_its_own_image(self):
        self.env["ECR_REPOSITORY"] = "ipgo-dev/ai"
        self.present["images"][0]["repositoryName"] = "ipgo-dev/ai"
        self.assertIn("/ipgo-dev/ai:", self.resolve(self.present)["image"])

    def test_amd64_and_arm64_tags_are_not_interchangeable(self):
        self.env.update(COMPUTE_MODE="legacy", IMAGE_TAG=SHA)
        with self.assertRaises(MODULE.ImageLookupError):
            self.resolve(self.present)
        self.present["images"][0]["imageId"]["imageTag"] = SHA
        self.assertTrue(self.resolve(self.present)["image"].endswith(":" + SHA))

    def test_unapproved_account_region_repository_or_sha_never_queries(self):
        changes = [
            {"AWS_ACCOUNT_ID": "bad"}, {"ECR_REGISTRY": "123456789012.dkr.ecr.us-east-1.amazonaws.com"},
            {"ECR_REPOSITORY": "ipgo-production/ai"}, {"GITHUB_SHA": "short"}, {"IMAGE_TAG": "latest"},
            {"COMPUTE_MODE": "unknown"}, {"EXPECTED_IMAGE_DIGEST": "sha256:short"},
        ]
        for change in changes:
            with self.subTest(change=change):
                lookup = unittest.mock.Mock()
                with self.assertRaises(MODULE.ImageLookupError):
                    MODULE.resolve({**self.env, **change}, lookup=lookup)
                lookup.assert_not_called()

    def test_failed_lookup_is_never_treated_as_missing(self):
        for code in ["AccessDeniedException", "RepositoryNotFoundException", "UpstreamTooManyRequests"]:
            response = copy.deepcopy(self.missing)
            response["failures"][0]["failureCode"] = code
            with self.subTest(code=code), self.assertRaises(MODULE.ImageLookupError):
                self.resolve(response)

    def test_empty_partial_or_ambiguous_response_stops(self):
        for response in [{}, {"images": [], "failures": []}, {"images": []}, [], None]:
            with self.subTest(response=response), self.assertRaises(MODULE.ImageLookupError):
                self.resolve(response)

    def test_response_identity_must_match_exact_requested_target(self):
        for field, wrong in [("registryId", "987654321098"), ("repositoryName", "ipgo-dev/ai")]:
            response = copy.deepcopy(self.present)
            response["images"][0][field] = wrong
            with self.subTest(field=field), self.assertRaises(MODULE.ImageLookupError):
                self.resolve(response)
        response = copy.deepcopy(self.missing)
        response["failures"][0]["imageId"]["imageTag"] = SHA
        with self.assertRaises(MODULE.ImageLookupError):
            self.resolve(response)

    def test_malformed_digest_is_rejected(self):
        for digest in ["", "latest", "sha256:bad", "sha256:" + "b" * 64 + "\n", None]:
            response = copy.deepcopy(self.present)
            response["images"][0]["imageId"]["imageDigest"] = digest
            with self.subTest(digest=digest), self.assertRaises(MODULE.ImageLookupError):
                self.resolve(response)

    def test_final_check_requires_an_available_image(self):
        with self.assertRaises(MODULE.ImageLookupError):
            self.resolve(self.missing, require_present=True)

    def test_digest_cannot_change_between_reuse_and_deploy(self):
        self.env["EXPECTED_IMAGE_DIGEST"] = DIGEST
        self.assertEqual(self.resolve(self.present, require_present=True)["digest"], DIGEST)
        changed = copy.deepcopy(self.present)
        changed["images"][0]["imageId"]["imageDigest"] = "sha256:" + "c" * 64
        for response in [changed, self.missing]:
            with self.assertRaises(MODULE.ImageLookupError):
                self.resolve(response, require_present=True)

    def test_no_mixed_success_failure_or_multiple_images(self):
        mixed = copy.deepcopy(self.present)
        mixed["failures"] = self.missing["failures"]
        multiple = copy.deepcopy(self.present)
        multiple["images"] *= 2
        for response in [mixed, multiple]:
            with self.assertRaises(MODULE.ImageLookupError):
                self.resolve(response)

    def test_cli_auth_network_and_json_errors_stop_without_leaking_stderr(self):
        failures = [
            subprocess.CalledProcessError(254, ["aws"], stderr="synthetic-private-error"),
            subprocess.TimeoutExpired(["aws"], 30), FileNotFoundError(),
        ]
        for error in failures:
            with self.subTest(error=type(error).__name__):
                with patch.object(MODULE.subprocess, "run", side_effect=error):
                    with self.assertRaises(MODULE.ImageLookupError) as caught:
                        MODULE.batch_get_image(ACCOUNT, "ipgo-dev/server", SHA + "-arm64")
                    self.assertNotIn("synthetic-private-error", str(caught.exception))
        with patch.object(MODULE.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "not-json")):
            with self.assertRaises(MODULE.ImageLookupError):
                MODULE.batch_get_image(ACCOUNT, "ipgo-dev/server", SHA + "-arm64")

    def test_lookup_only_uses_existing_read_permission_in_pinned_region(self):
        with patch.object(MODULE.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}")) as run:
            MODULE.batch_get_image(ACCOUNT, "ipgo-dev/server", SHA + "-arm64")
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["aws", "ecr", "batch-get-image"])
        self.assertEqual(args[args.index("--region") + 1], "ap-northeast-2")
        self.assertEqual(args[args.index("--registry-id") + 1], ACCOUNT)
        self.assertEqual(args[args.index("--image-ids") + 1], "imageTag=" + SHA + "-arm64")

    def invoke(self, response, options=(), exit_code=0):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aws = root / "aws"
            aws.write_text(
                f"#!{sys.executable}\nimport os,sys\n"
                "print(os.environ['STUB_RESPONSE'])\n"
                "print('synthetic-stderr-not-for-log',file=sys.stderr)\n"
                "sys.exit(int(os.environ['STUB_EXIT']))\n"
            )
            aws.chmod(0o700)
            output = root / "output"
            env = {
                **self.env, "PATH": str(root), "GITHUB_OUTPUT": str(output),
                "STUB_RESPONSE": json.dumps(response), "STUB_EXIT": str(exit_code),
            }
            result = subprocess.run(
                [sys.executable, str(SCRIPT), *options], env=env, capture_output=True, text=True,
            )
            return result, output.read_text() if output.exists() else ""

    def test_cli_emits_github_outputs_and_no_manifest_or_registry_log(self):
        result, outputs = self.invoke(self.present)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exists=true\n", outputs)
        self.assertIn("digest=" + DIGEST, outputs)
        self.assertNotIn(ACCOUNT, result.stdout + result.stderr)
        self.assertNotIn("schemaVersion", result.stdout + result.stderr)
        self.assertNotIn("synthetic-stderr", result.stdout + result.stderr)

    def test_cli_missing_and_post_build_paths(self):
        result, outputs = self.invoke(self.missing)
        self.assertEqual(result.returncode, 0)
        self.assertIn("exists=false\n", outputs)
        result, outputs = self.invoke(self.present, ("--require-present",))
        self.assertEqual(result.returncode, 0)
        self.assertIn("exists=true\n", outputs)

    def test_cli_errors_do_not_publish_a_build_decision(self):
        for response, options, exit_code in [
            (self.present, (), 254), ({}, (), 0), (self.missing, ("--require-present",), 0),
        ]:
            with self.subTest(exit_code=exit_code, options=options):
                result, outputs = self.invoke(response, options, exit_code)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(outputs, "")
                self.assertNotIn("synthetic-stderr", result.stderr)

    def test_actual_workflow_skips_both_build_steps_and_rechecks_before_render(self):
        workflow = (ROOT / ".github/workflows/deploy-dev.yml").read_text()
        condition = "if: steps.image_lookup.outputs.exists == 'false'"
        self.assertEqual(workflow.count(condition), 2)
        lookup = workflow.index("id: image_lookup")
        verify = workflow.index("id: verified_image")
        render = workflow.index("      - name: Render")
        self.assertLess(lookup, verify)
        self.assertLess(verify, render)
        self.assertIn("EXPECTED_IMAGE_DIGEST: ${{ steps.image_lookup.outputs.digest }}", workflow)
        self.assertIn('python3 "$RUNNER_TEMP/check_ecr_image.py" --require-present', workflow)
        self.assertIn("tags: ${{ steps.image_lookup.outputs.image }}", workflow)
        self.assertIn("${{ steps.verified_image.outputs.image }}", workflow)
        self.assertNotIn("ecr batch-delete-image", workflow)
        self.assertNotIn("put-image-tag-mutability", workflow)


if __name__ == "__main__":
    unittest.main()
