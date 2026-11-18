"""LGBM19는 비공개 사전 빌드 이미지가 없으면 기본 빌드로 대체하지 않는다."""
import argparse
import json
import os
import re
from pathlib import Path


def validate(env):
    model_insight = env.get("MODEL_INSIGHT_ENABLED", "false")
    if model_insight not in ("true", "false"):
        raise ValueError("MODEL_INSIGHT_ENABLED must be true or false")
    if model_insight == "true" and not (
        env.get("MODEL19_ENABLED") == "true"
        and env.get("LLM_ENABLED") == "true"
        and env.get("LLM_APPROVED") == "true"
    ):
        raise ValueError("Model explanations require model19 and explicit LLM approval")
    if env.get("MODEL19_ENABLED", "false") != "true":
        return
    if (env.get("ENGINE_MODE") != "real" or env.get("IMAGE_EXISTS") != "true"
            or not re.fullmatch(r"lgbm19-[a-f0-9]{16}", env.get("MODEL19_VERSION", ""))):
        raise ValueError("LGBM19 requires a real engine, existing immutable private image, and fixed version")


def configure(directory, enabled, model_insight_enabled=False):
    if model_insight_enabled and not enabled:
        raise ValueError("Model explanations require model19")
    paths = [directory / f"ai-{kind}-task-definition.json" for kind in ("api", "worker")]
    if not paths[0].is_file():
        raise ValueError("API task definition snapshot is missing")
    for path in paths:
        if not path.exists():
            continue
        task = json.loads(path.read_text())
        for container in task["containerDefinitions"]:
            if container["name"] not in ("ai-api", "ai-worker"):
                continue
            values = [v for v in container.get("environment", [])
                      if v["name"] not in ("IPGO_MODEL19_DIR", "IPGO_MODEL_INSIGHT_ENABLED")]
            values.append({"name": "IPGO_MODEL_INSIGHT_ENABLED",
                           "value": str(model_insight_enabled).lower()})
            if enabled:
                values.append({"name":"IPGO_MODEL19_DIR", "value":"/opt/ipgo-model19"})
            container["environment"] = values
        path.write_text(json.dumps(task)+"\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("guard", "configure"))
    args = parser.parse_args()
    if args.mode == "guard":
        validate(os.environ)
    else:
        configure(Path(os.environ["RUNNER_TEMP"]), os.getenv("MODEL19_ENABLED") == "true",
                  os.getenv("MODEL_INSIGHT_ENABLED", "false") == "true")


if __name__ == "__main__":
    main()
