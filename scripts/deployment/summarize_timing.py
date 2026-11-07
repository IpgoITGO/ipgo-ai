"""GitHub 실행 메타데이터만 읽어 배포 시간을 요약한다. AWS 호출은 없다."""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen


def instant(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else None


def elapsed(start, end):
    start, end = instant(start), instant(end)
    if start is None or end is None or end < start:
        return "미확인"
    return f"{(end - start).total_seconds():.0f}초"


def safe_cell(value):
    return re.sub(r"[|\r\n<>]", " ", str(value))[:160]


def render(run, jobs, env, now):
    attempt = int(env["GITHUB_RUN_ATTEMPT"])
    matching = [
        job for job in jobs if job.get("name") == env["GITHUB_JOB"] and job.get("run_attempt", attempt) == attempt
    ]
    if len(matching) != 1:
        raise ValueError("Current job is not uniquely available")
    job = matching[0]
    # 재실행은 첫 시도의 created_at을 대기 시간에 합산하지 않는다.
    origin = run.get("created_at") if attempt == 1 else run.get("run_started_at")
    reused = {"true": "기존 SHA 이미지 재사용", "false": "새 이미지 빌드 경로"}.get(
        env.get("IMAGE_REUSED"), "이미지 조회 결과 미확인"
    )
    status = env.get("DEPLOY_STATUS", "unknown")
    if status not in {"success", "failure", "cancelled", "unknown"}:
        status = "unknown"
    lines = [
        "## 개발 배포 시간",
        "",
        f"- 요약 직전 job 상태: `{status}`",
        f"- 이미지: {reused}",
        f"- 시도 {attempt} 시작 기준 → job 시작: **{elapsed(origin, job.get('started_at'))}**",
        "  (환경 승인·concurrency 대기·러너 할당 합계이며 각각의 시간은 분리할 수 없음)",
        f"- job 시작 → 요약 생성: **{elapsed(job.get('started_at'), now)}**",
        "  (이 요약 및 post 처리 이후의 전체 종료 시간은 포함하지 않음)",
        "",
        "| 단계 | 소요 시간 | 결과 |",
        "|---|---:|---|",
    ]
    for step in job.get("steps", []):
        name = step.get("name", "")
        if name.startswith("Post ") or name in {"Summarize deployment timing", "Complete job"}:
            continue
        state = step.get("conclusion")
        if state == "skipped":
            duration, state = "—", "생략"
        elif step.get("status") != "completed":
            duration, state = "미확인", "API 집계 중"
        else:
            duration = elapsed(step.get("started_at"), step.get("completed_at"))
            state = state or "미확인"
        lines.append(f"| {safe_cell(name)} | {duration} | {safe_cell(state)} |")
    lines += [
        "",
        "ECS 단계는 task 등록·교체·안정화 확인 합계이며 앱 기동 시간만을 뜻하지 않습니다.",
        "API 반영 지연으로 미확인인 단계는 실행 종료 후 Actions의 job 기록에서 확인합니다.",
        "",
    ]
    return "\n".join(lines)


def collect(env, get):
    repository = env["GITHUB_REPOSITORY"]
    run_id, attempt = env["GITHUB_RUN_ID"], env["GITHUB_RUN_ATTEMPT"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    if not run_id.isdecimal() or not attempt.isdecimal() or int(attempt) < 1:
        raise ValueError("Invalid run identity")
    base = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}"
    run = get(base)
    if int(run["run_attempt"]) != int(attempt):
        raise ValueError("Run attempt changed")
    jobs = []
    for page in range(1, 11):
        batch = get(f"{base}/attempts/{attempt}/jobs?per_page=100&page={page}")["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            return run, jobs
    raise ValueError("Incomplete pagination")


def main(env=None, get=None, now=None):
    env = os.environ if env is None else env
    try:
        if get is None:
            token = env["GH_TOKEN"]

            def get(url):
                request = Request(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                with urlopen(request, timeout=10) as response:
                    return json.load(response)

        run, jobs = collect(env, get)
        content = render(run, jobs, env, now or datetime.now(UTC).isoformat())
    except Exception:
        # token·API 응답·URL·예외 원문을 로그/요약에 넣지 않는다.
        content = (
            "## 개발 배포 시간\n\n시간 정보 조회 불가. API 권한·반영 지연·checkout 상태를 "
            "확인하고 Actions job 기록에서 단계별 시간을 확인하세요.\n"
            "이 결과는 배포 성공/실패 판정을 변경하지 않습니다.\n"
        )
    target = env.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as summary:
            summary.write(content)
    else:
        print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
