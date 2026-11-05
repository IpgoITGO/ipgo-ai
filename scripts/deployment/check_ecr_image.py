"""Read-only ECR lookup for a fixed dev SHA. Never turns lookup errors into builds."""

import argparse
import json
import os
import re
import subprocess
import sys


class ImageLookupError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ImageLookupError(message)


def identity(env):
    account = env.get("AWS_ACCOUNT_ID", "")
    require(re.fullmatch(r"[0-9]{12}", account), "Invalid registry account")
    registry = env.get("ECR_REGISTRY", "")
    require(registry == f"{account}.dkr.ecr.ap-northeast-2.amazonaws.com", "Registry mismatch")
    repository = env.get("ECR_REPOSITORY", "")
    require(repository in ("ipgo-dev/server", "ipgo-dev/ai"), "Unsupported dev repository")
    sha = env.get("GITHUB_SHA", "")
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "A full workflow SHA is required")
    mode = env.get("COMPUTE_MODE", "")
    require(mode in ("", "legacy", "onebox"), "Unsupported compute mode")
    tag = sha + ("-arm64" if mode == "onebox" else "")
    require(env.get("IMAGE_TAG") == tag, "Image tag does not match SHA and architecture")
    expected = env.get("EXPECTED_IMAGE_DIGEST", "")
    require(not expected or re.fullmatch(r"sha256:[0-9a-f]{64}", expected), "Invalid expected digest")
    return account, registry, repository, tag, expected


def batch_get_image(account, repository, tag):
    try:
        response = subprocess.run(
            [
                "aws", "ecr", "batch-get-image", "--region", "ap-northeast-2",
                "--registry-id", account, "--repository-name", repository,
                "--image-ids", f"imageTag={tag}", "--output", "json", "--no-cli-pager",
            ],
            capture_output=True, text=True, check=True, timeout=30,
            env={**os.environ, "AWS_MAX_ATTEMPTS": "1"},
        )
        return json.loads(response.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ImageLookupError("ECR lookup failed; refusing to build or deploy") from None


def resolve(env, require_present=False, lookup=batch_get_image):
    account, registry, repository, tag, expected = identity(env)
    response = lookup(account, repository, tag)
    require(isinstance(response, dict), "Invalid ECR response")
    images = response.get("images")
    failures = response.get("failures")
    require(isinstance(images, list) and isinstance(failures, list), "Incomplete ECR response")
    image_uri = f"{registry}/{repository}:{tag}"
    if len(images) == 1 and not failures:
        image = images[0]
        require(isinstance(image, dict), "Invalid ECR image")
        require(image.get("registryId") == account and image.get("repositoryName") == repository,
                "ECR response target mismatch")
        image_id = image.get("imageId", {})
        require(isinstance(image_id, dict) and image_id.get("imageTag") == tag, "ECR tag mismatch")
        digest = image_id.get("imageDigest", "")
        require(isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "Invalid image digest")
        require(not expected or digest == expected, "Existing image digest changed during deployment")
        return {"exists": "true", "image": image_uri, "digest": digest}
    if not images and len(failures) == 1:
        failure = failures[0]
        require(isinstance(failure, dict), "Invalid ECR failure")
        require(failure.get("failureCode") == "ImageNotFound"
                and failure.get("imageId") == {"imageTag": tag}, "ECR lookup did not confirm absence")
        require(not require_present and not expected, "Required image is missing; refusing deployment")
        return {"exists": "false", "image": image_uri, "digest": ""}
    raise ImageLookupError("Ambiguous ECR lookup; refusing to build or deploy")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-present", action="store_true")
    args = parser.parse_args()
    try:
        result = resolve(os.environ, require_present=args.require_present)
        output = os.environ.get("GITHUB_OUTPUT")
        require(bool(output), "GitHub output file is required")
        # URI/manifest/CLI stderr are not printed. The immutable SHA tag remains the deployment reference.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w") as stream:
            for key, value in result.items():
                stream.write(f"{key}={value}\n")
        print(json.dumps({"image_exists": result["exists"] == "true", "digest": result["digest"]}))
    except (ImageLookupError, OSError, TypeError):
        print("::error::ECR SHA image lookup rejected; no fallback build on lookup errors.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
