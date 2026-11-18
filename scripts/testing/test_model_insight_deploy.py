import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("model19_gate", ROOT / "scripts/deployment/model19_gate.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class ModelInsightDeploymentTests(unittest.TestCase):
    def test_on_requires_model_real_image_and_explicit_paid_llm_approval(self):
        env = dict(MODEL_INSIGHT_ENABLED="true", MODEL19_ENABLED="true", LLM_ENABLED="true",
                   LLM_APPROVED="true", ENGINE_MODE="real", IMAGE_EXISTS="true",
                   MODEL19_VERSION="lgbm19-0123456789abcdef")
        M.validate(env)
        for name in ("MODEL19_ENABLED", "LLM_ENABLED", "LLM_APPROVED", "IMAGE_EXISTS"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                M.validate(dict(env, **{name: "false"}))
        for value in ("", "TRUE", "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                M.validate(dict(env, MODEL_INSIGHT_ENABLED=value))

    def test_default_off_and_rollback_preserve_image_other_settings_and_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ai-api-task-definition.json"
            original = {"family": "synthetic", "containerDefinitions": [{"name": "ai-api",
                "image": "synthetic:immutable", "secrets": [{"name": "OPENAI_API_KEY", "valueFrom": "synthetic-ref"}],
                "environment": [{"name": "KEEP", "value": "yes"},
                                {"name": "IPGO_MODEL_INSIGHT_ENABLED", "value": "true"}]}]}
            path.write_text(json.dumps(original))
            for enabled in (False, True, False):
                M.configure(Path(temp), True, enabled)
                container = json.loads(path.read_text())["containerDefinitions"][0]
                values = {e["name"]: e["value"] for e in container["environment"]}
                self.assertEqual(values["IPGO_MODEL_INSIGHT_ENABLED"], str(enabled).lower())
                self.assertEqual(values["IPGO_MODEL19_DIR"], "/opt/ipgo-model19")
                self.assertEqual(values["KEEP"], "yes")
                self.assertEqual(container["image"], original["containerDefinitions"][0]["image"])
                self.assertEqual(container["secrets"], original["containerDefinitions"][0]["secrets"])
            M.configure(Path(temp), True)
            self.assertIn({"name": "IPGO_MODEL_INSIGHT_ENABLED", "value": "false"},
                          json.loads(path.read_text())["containerDefinitions"][0]["environment"])
            with self.assertRaises(ValueError):
                M.configure(Path(temp), False, True)

    def test_workflow_passes_explicit_switch_and_approval_to_gate(self):
        workflow = (ROOT / ".github/workflows/deploy-dev.yml").read_text()
        for field in ("MODEL_INSIGHT_ENABLED: ${{ inputs.model_insight_enabled }}",
                      "LLM_ENABLED: ${{ inputs.llm_enabled }}", "LLM_APPROVED: ${{ inputs.llm_approved }}"):
            self.assertIn(field, workflow)


if __name__ == "__main__":
    unittest.main()
