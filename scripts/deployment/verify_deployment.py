"""Fail-closed deployment evidence checks. Only GET, git reads and AWS read APIs."""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REGION = "ap-northeast-2"
CLUSTER = "ipgo-dev-cluster"
EVIDENCE_ERRORS = (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError, subprocess.SubprocessError)
DB_PATHS = (
    "database/**",
    "Dockerfile.database",
    ".dockerignore",
    "scripts/database-task.sh",
    "scripts/verify-database-task-image.sh",
    "scripts/verify-postgres-migrations.sh",
    "scripts/verify-insight-history-upgrade.sh",
    "scripts/verify-billing-ledger.sh",
    "scripts/testing/verify-receipt-matching.sh",
    "scripts/testing/verify-monthly-insight-budget.sh",
    "scripts/testing/verify-insight-contract.sh",
    "scripts/testing/insight_fake_api.py",
    "scripts/testing/billing-ledger-invariants.sql",
    "src/main/resources/db/migration/**",
    "src/test/**",
    "build.gradle",
    "settings.gradle",
    "gradle/**",
    "gradlew",
    ".github/workflows/database.yml",
)

JOBS = {
    "server": {"deploy-contract", "test", "arm-test", "container"},
    "ai": {"deploy-contract", "test", "arm-capacity", "container"},
}
READ_OPERATIONS = {
    ("sts", "get-caller-identity"),
    ("ecs", "describe-services"),
    ("ecs", "list-tasks"),
    ("ecs", "describe-tasks"),
    ("ecs", "describe-task-definition"),
    ("ecr", "batch-get-image"),
}


class GateError(ValueError):
    pass


def require(value, message):
    if not value:
        raise GateError(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GateError("Redirected verification request rejected")


def http_json(url, token=None):
    headers = {"Accept": "application/json"}
    if token:
        headers.update(Authorization=f"Bearer {token}", **{"X-GitHub-Api-Version": "2022-11-28"})
    with build_opener(NoRedirect).open(Request(url, headers=headers), timeout=10) as response:
        require(response.status == 200, "HTTP verification did not return 200")
        body = response.read(4 * 1024 * 1024 + 1)
        require(len(body) <= 4 * 1024 * 1024, "Verification response too large")
        return json.loads(body)


def pages(get, path, key=None, params=None):
    values = []
    for page in range(1, 11):
        payload = get(path + "?" + urlencode({**(params or {}), "per_page": 100, "page": page}))
        batch = payload[key] if key else payload
        require(isinstance(batch, list), "Invalid paginated evidence")
        values.extend(batch)
        if len(batch) < 100:
            return values
    raise GateError("Evidence pagination incomplete")


def workflow_success(get, repo, workflow, sha, event, required, branch=None):
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "CI source SHA is invalid")
    params = {"head_sha": sha, "event": event}
    if branch:
        params["branch"] = branch
    runs = pages(get, f"/repos/{repo}/actions/workflows/{workflow}/runs", "workflow_runs", params)
    require(runs, "Required CI run is missing")
    # A newer failed, cancelled or pending run must never fall back to older green evidence.
    run = max(runs, key=lambda item: (item.get("updated_at") or item.get("run_started_at") or "", int(item["id"])))
    require(run["head_sha"] == sha and run["event"] == event, "CI source or event mismatch")
    require(run["path"] == f".github/workflows/{workflow}", "CI workflow mismatch")
    require(run["head_repository"]["full_name"] == repo, "CI source repository mismatch")
    require(not branch or run["head_branch"] == branch, "CI branch mismatch")
    require(
        run["status"] == "completed" and run["conclusion"] == "success",
        "Latest required CI is not successful",
    )
    attempt = int(run["run_attempt"])
    jobs = pages(get, f"/repos/{repo}/actions/runs/{run['id']}/attempts/{attempt}/jobs", "jobs")
    for name in required:
        matches = [job for job in jobs if job["name"] == name]
        require(len(matches) == 1, "Required CI job missing or ambiguous")
        job = matches[0]
        require(
            job["run_attempt"] == attempt and job["status"] == "completed" and job["conclusion"] == "success",
            "Required CI job is not successful",
        )
    return int(run["id"])


def database_required(files):
    return any(fnmatch.fnmatchcase(path, pattern) for path in files for pattern in DB_PATHS)


def changed_paths(sha, cwd=None):
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "Invalid approved source SHA")
    # Keep both sides of a rename, including a DB input moved out of its scope.
    result = subprocess.run(
        ["git", "diff", "--no-renames", f"{sha}^1", sha, "--name-only", "-z"],
        cwd=cwd,
        capture_output=True,
        check=True,
        timeout=15,
    )
    return [path for path in result.stdout.decode().split("\0") if path]


def pr_paths(files):
    paths = []
    for file in files:
        for key in ("filename", "previous_filename"):
            if key == "filename" or key in file:
                path = file[key]
                require(isinstance(path, str) and path, "Invalid PR file path evidence")
                paths.append(path)
    return paths


def validate_url(url):
    parsed = urlsplit(url)
    require(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.port in (None, 443),
        "A fixed HTTPS smoke URL without credentials, query or fragment is required",
    )


def ci_check(env, service, get, changed_files):
    repo = f"IpgoITGO/ipgo-{service}"
    require(env.get("GITHUB_REPOSITORY") == repo, "Deployment repository mismatch")
    sha = env.get("EXPECTED_SHA", "")
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "A full approved source SHA is required")
    require(sha == env.get("DEPLOYMENT_SOURCE_SHA"), "Approved source mismatch")
    count = desired(env, "DESIRED_COUNT" if service == "server" else "API_DESIRED_COUNT")
    url = env.get("DEPLOYMENT_SMOKE_URL", "")
    require(
        service != "server" or count == 0 or url,
        "Server HTTPS smoke URL is required before deployment",
    )
    if url:
        validate_url(url)
    evidence = {"test_run": workflow_success(get, repo, "test.yml", sha, "push", JOBS[service], "dev")}
    if service == "server":
        prs = pages(get, f"/repos/{repo}/commits/{sha}/pulls")
        merged = [
            p
            for p in prs
            if p.get("merged_at")
            and p.get("merge_commit_sha") == sha
            and p["base"]["ref"] == "dev"
            and p["head"]["repo"]["full_name"] == repo
        ]
        require(len(merged) == 1, "Server source requires a unique merged dev PR")
        files = pages(get, f"/repos/{repo}/pulls/{merged[0]['number']}/files")
        require(files, "Merged PR file evidence is missing")
        # Full PR files cover squash/rebase and multi-commit changes beyond the last parent diff.
        if database_required([*changed_files, *pr_paths(files)]):
            evidence["database_run"] = workflow_success(
                get, repo, "database.yml", merged[0]["head"]["sha"], "pull_request", {"verify"}
            )
    return evidence


def desired(env, key):
    value = env.get(key, "")
    require(value in {"0", "1", "2"}, "Invalid approved task count")
    return int(value)


def aws_read(service, operation, **params):
    require(
        (service, operation) in READ_OPERATIONS,
        "AWS mutation or unsupported read rejected",
    )
    response = subprocess.run(
        [
            "aws",
            service,
            operation,
            "--region",
            REGION,
            "--output",
            "json",
            "--cli-input-json",
            json.dumps(params),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, "AWS_PAGER": "", "AWS_MAX_ATTEMPTS": "1"},
    )
    return json.loads(response.stdout)


def task_list(read, name):
    response = read("ecs", "list-tasks", cluster=CLUSTER, serviceName=name, desiredStatus="RUNNING")
    require(not response.get("nextToken"), "Task pagination incomplete")
    arns = response["taskArns"]
    if not arns:
        return []
    tasks = read("ecs", "describe-tasks", cluster=CLUSTER, tasks=arns)
    require(
        not tasks.get("failures") and len(tasks["tasks"]) == len(arns),
        "Task inspection incomplete",
    )
    return tasks["tasks"]


def preflight(env, service, read):
    require(env.get("COMPUTE_MODE", "") in {"", "legacy", "onebox"}, "Invalid compute mode")
    account = env.get("AWS_ACCOUNT_ID", "")
    require(re.fullmatch(r"[0-9]{12}", account), "Expected AWS account is required")
    identity = read("sts", "get-caller-identity")
    require(identity["Account"] == account, "AWS account mismatch")
    name = service_name(env, service)
    data = read("ecs", "describe-services", cluster=CLUSTER, services=[name])
    require(
        not data.get("failures") and len(data["services"]) == 1,
        "Target service inspection failed",
    )
    require(
        data["services"][0]["status"] == "ACTIVE" and data["services"][0]["serviceName"] == name,
        "Target service is not active or has the wrong identity",
    )
    # Detect missing read permissions before ECR push or ECS changes.
    task_list(read, name)
    return {"read_preflight": "passed"}


def service_name(env, service):
    suffix = "server" if service == "server" else "ai-api"
    prefix = "ipgo-dev-onebox-" if env.get("COMPUTE_MODE") == "onebox" else "ipgo-dev-"
    name = prefix + suffix
    require(env.get("ECS_SERVICE_NAME") == name, "Unexpected target service")
    return name


def template_targets(env, service):
    name = service_name(env, service)
    if service == "server":
        return {name: "task-definition.json"}
    targets = {name: "ai-api-task-definition.json"}
    if env.get("COMPUTE_MODE") != "onebox":
        targets["ipgo-dev-ai-worker"] = "ai-worker-task-definition.json"
    return targets


def require_template_arn(env, name, arn):
    require(
        re.fullmatch(r"[0-9]{12}", env.get("AWS_ACCOUNT_ID", ""))
        and isinstance(arn, str)
        and re.fullmatch(
            rf"arn:aws:ecs:{REGION}:{env['AWS_ACCOUNT_ID']}:task-definition/{name}:[0-9]+", arn
        ),
        "Active task definition has the wrong account, region or family",
    )


def active_template_arn(env, name, read):
    data = read("ecs", "describe-services", cluster=CLUSTER, services=[name])
    require(not data.get("failures") and len(data["services"]) == 1, "Template service inspection failed")
    current = data["services"][0]
    require(current["status"] == "ACTIVE" and current["serviceName"] == name, "Unexpected template service")
    arn = current["taskDefinition"]
    require_template_arn(env, name, arn)
    deployments = current["deployments"]
    require(
        len(deployments) == 1
        and deployments[0]["status"] == "PRIMARY"
        and deployments[0]["taskDefinition"] == arn
        and deployments[0].get("rolloutState") == "COMPLETED",
        "Template service has an incomplete or concurrent deployment",
    )
    return arn


def capture_templates(env, service, read):
    templates, snapshot = {}, {}
    for name, filename in template_targets(env, service).items():
        arn = active_template_arn(env, name, read)
        definition = read("ecs", "describe-task-definition", taskDefinition=arn)["taskDefinition"]
        require(
            definition["taskDefinitionArn"] == arn and definition["family"] == name,
            "Active task definition response mismatch",
        )
        templates[filename], snapshot[name] = definition, arn
    return templates, snapshot


def check_templates(env, service, read, snapshot):
    require(
        isinstance(snapshot, dict) and set(snapshot) == set(template_targets(env, service)),
        "Template snapshot mismatch",
    )
    for name, arn in snapshot.items():
        require_template_arn(env, name, arn)
        require(active_template_arn(env, name, read) == arn, "Active task definition changed before deployment")
    return {"active_templates_unchanged": True}


def write_private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream)


def image_digests(read, service, env):
    digest = env.get("EXPECTED_IMAGE_DIGEST", "")
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
        "Expected image digest is required",
    )
    sha = env.get("EXPECTED_SHA", "")
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "Approved image source SHA is required")
    suffix = "-arm64" if env.get("COMPUTE_MODE") == "onebox" else ""
    expected = f"{env['AWS_ACCOUNT_ID']}.dkr.ecr.{REGION}.amazonaws.com/ipgo-dev/{service}:{sha}{suffix}"
    require(
        env.get("EXPECTED_IMAGE") == expected,
        "Approved source image reference mismatch",
    )
    response = read(
        "ecr",
        "batch-get-image",
        repositoryName=f"ipgo-dev/{service}",
        imageIds=[{"imageDigest": digest}],
    )
    require(
        not response.get("failures") and len(response["images"]) == 1,
        "Image manifest inspection failed",
    )
    image = response["images"][0]
    require(image["imageId"]["imageDigest"] == digest, "Image manifest digest mismatch")
    manifest = json.loads(image["imageManifest"])
    if "manifests" not in manifest:
        return {digest}
    architecture = "arm64" if env.get("COMPUTE_MODE") == "onebox" else "amd64"
    platform = [
        m["digest"]
        for m in manifest["manifests"]
        if m.get("platform", {}).get("architecture") == architecture and m.get("platform", {}).get("os") == "linux"
    ]
    require(
        len(platform) == 1 and re.fullmatch(r"sha256:[0-9a-f]{64}", platform[0]),
        "Runtime platform manifest is missing or ambiguous",
    )
    return {digest, platform[0]}


def verify_service(read, env, name, td_arn, count, container, digests, api=True):
    require(
        re.fullmatch(
            rf"arn:aws:ecs:{REGION}:{env['AWS_ACCOUNT_ID']}:task-definition/{name}:[0-9]+",
            td_arn,
        ),
        "Expected task definition ARN is missing or has the wrong target",
    )
    data = read("ecs", "describe-services", cluster=CLUSTER, services=[name])
    require(
        not data.get("failures") and len(data["services"]) == 1,
        "Service evidence incomplete",
    )
    current = data["services"][0]
    require(
        current["status"] == "ACTIVE" and current["serviceName"] == name and current["taskDefinition"] == td_arn,
        "Candidate task definition is not active; possible rollback",
    )
    require(
        current["desiredCount"] == count and current["runningCount"] == count and current["pendingCount"] == 0,
        "Approved replica counts are not stable",
    )
    deployments = current["deployments"]
    require(
        len(deployments) == 1
        and deployments[0]["status"] == "PRIMARY"
        and deployments[0]["taskDefinition"] == td_arn
        and deployments[0].get("rolloutState") == "COMPLETED",
        "Candidate rollout is not completed",
    )
    td = read("ecs", "describe-task-definition", taskDefinition=td_arn)["taskDefinition"]
    require(td["taskDefinitionArn"] == td_arn, "Task definition evidence mismatch")
    definitions = [c for c in td["containerDefinitions"] if c["name"] == container]
    require(
        len(definitions) == 1 and definitions[0]["image"] == env["EXPECTED_IMAGE"],
        "Task image reference mismatch",
    )
    health = definitions[0].get("healthCheck")
    if api and count:
        path = "8080/actuator/health" if container == "server" else "8000/health"
        require(
            health and f"http://127.0.0.1:{path}" in " ".join(health["command"]),
            "Application HTTP container health check is required",
        )
    tasks = task_list(read, name)
    require(len(tasks) == count, "Running task inventory mismatch")
    for task in tasks:
        require(
            task["taskDefinitionArn"] == td_arn and task["lastStatus"] == "RUNNING",
            "Old or stopped task remains",
        )
        matches = [c for c in task["containers"] if c["name"] == container]
        require(len(matches) == 1, "Application container missing or ambiguous")
        app = matches[0]
        require(
            app["lastStatus"] == "RUNNING" and app.get("imageDigest") in digests,
            "Running image digest mismatch",
        )
        if api or health:
            require(
                app.get("healthStatus") == "HEALTHY",
                "Application health is not HEALTHY",
            )
        if api:
            require(task.get("healthStatus") == "HEALTHY", "Task health is not HEALTHY")
    return {
        "replicas": count,
        "application_health_checked": bool(count and (api or health)),
    }


def post_check(env, service, read, smoke):
    name = service_name(env, service)
    count = desired(env, "DESIRED_COUNT" if service == "server" else "API_DESIRED_COUNT")
    digests = image_digests(read, service, env)
    result = {
        "api": verify_service(
            read,
            env,
            name,
            env.get("EXPECTED_TASK_DEFINITION", ""),
            count,
            "server" if service == "server" else "ai-api",
            digests,
        )
    }
    if service == "ai":
        workers = desired(env, "WORKER_DESIRED_COUNT")
        if env.get("COMPUTE_MODE") == "onebox":
            data = read(
                "ecs",
                "describe-services",
                cluster=CLUSTER,
                services=["ipgo-dev-ai-worker"],
            )
            require(
                not data.get("failures") and len(data["services"]) == 1,
                "Worker evidence incomplete",
            )
            worker = data["services"][0]
            require(
                workers == 0 and worker["desiredCount"] == worker["runningCount"] == worker["pendingCount"] == 0,
                "Separate worker must remain stopped",
            )
        else:
            result["worker"] = verify_service(
                read,
                env,
                "ipgo-dev-ai-worker",
                env.get("EXPECTED_WORKER_TASK_DEFINITION", ""),
                workers,
                "ai-worker",
                digests,
                api=False,
            )
    url = env.get("DEPLOYMENT_SMOKE_URL", "")
    if count and (url or service == "server"):
        validate_url(url)
        response = smoke(url)
        require(
            isinstance(response, dict) and response.get("status") == ("UP" if service == "server" else "ok"),
            "Application HTTP response is not healthy",
        )
        result["external_http"] = "passed"
    else:
        result["external_http"] = "not_run_scaled_down" if not count else "not_exposed_agent_http_health_checked"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("ci", "preflight", "snapshot", "check-template", "post"))
    parser.add_argument("--service", choices=("server", "ai"), required=True)
    args = parser.parse_args()
    try:
        if args.command == "ci":
            token = os.environ["GH_TOKEN"]
            sha = os.environ["EXPECTED_SHA"]
            require(re.fullmatch(r"[0-9a-f]{40}", sha), "Invalid approved source SHA")
            result = ci_check(
                os.environ,
                args.service,
                lambda path: http_json("https://api.github.com" + path, token),
                changed_paths(sha),
            )
        elif args.command == "preflight":
            result = preflight(os.environ, args.service, aws_read)
        elif args.command == "snapshot":
            templates, snapshot = capture_templates(os.environ, args.service, aws_read)
            directory = Path(os.environ["RUNNER_TEMP"])
            for filename, definition in templates.items():
                write_private_json(directory / filename, definition)
            write_private_json(directory / "source-task-definitions.json", snapshot)
            result = {"active_templates_captured": len(templates)}
        elif args.command == "check-template":
            snapshot = json.loads((Path(os.environ["RUNNER_TEMP"]) / "source-task-definitions.json").read_text())
            result = check_templates(os.environ, args.service, aws_read, snapshot)
        else:
            result = post_check(os.environ, args.service, aws_read, http_json)
        print(json.dumps(result))
        return 0
    except GateError as error:
        print(f"::error::{error}", file=sys.stderr)
    except EVIDENCE_ERRORS:
        # Never echo AWS/HTTP bodies, commands, tokens or exception strings.
        print(
            "::error::Deployment evidence unavailable; verification rejected",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
