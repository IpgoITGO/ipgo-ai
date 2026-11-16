import importlib.util
import json
from pathlib import Path

import pytest
import yaml

spec = importlib.util.spec_from_file_location("model19_gate", "scripts/deployment/model19_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_private_model_cannot_fall_back_to_default_image():
    good = dict(MODEL19_ENABLED="true", ENGINE_MODE="real", IMAGE_EXISTS="true",
                MODEL19_VERSION="lgbm19-"+"a"*16)
    gate.validate(good)
    for changes in ({"IMAGE_EXISTS":"false"}, {"MODEL19_VERSION":""}, {"ENGINE_MODE":"mock"}):
        with pytest.raises(ValueError):
            gate.validate(good | changes)
    gate.validate({"MODEL19_ENABLED":"false"})


def test_model_path_does_not_remove_other_env_or_secret_references(tmp_path):
    path = tmp_path / "ai-api-task-definition.json"
    original = dict(containerDefinitions=[dict(name="ai-api", memory=256,
                    environment=[dict(name="ENGINE_MODE", value="real")],
                    secrets=[dict(name="DATABASE_URL", valueFrom="synthetic-reference")])])
    path.write_text(json.dumps(original))
    gate.configure(tmp_path, True)
    configured = json.loads(path.read_text())["containerDefinitions"][0]
    assert configured["memory"] == 256 and configured["secrets"] == original["containerDefinitions"][0]["secrets"]
    assert dict((v["name"],v["value"]) for v in configured["environment"]) == {
        "ENGINE_MODE":"real", "IPGO_MODEL19_DIR":"/opt/ipgo-model19"}
    gate.configure(tmp_path, False)
    assert json.loads(path.read_text()) == original


def test_workflow_checks_model_before_build_and_task_changes():
    workflow = yaml.safe_load(Path(".github/workflows/deploy-dev.yml").read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [s.get("name") for s in steps]
    assert names.index("Require prebuilt model19 image") < names.index("Build and push immutable image")
    assert names.index("Verify private model bundle") < names.index("Capture active service task definitions")
    assert names.index("Configure model19 path") < names.index("Render API task definition")
    checks = steps[names.index("Verify private model bundle")]["run"]
    assert '"$MODEL_IMAGE" uvicorn --version' in checks
    assert checks.index("uvicorn --version") < checks.index("load_bundle")
