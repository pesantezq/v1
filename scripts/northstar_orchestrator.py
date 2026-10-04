from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / ".agent" / "mission_registry.yaml"
PROJECT_STATE_PATH = REPO_ROOT / ".agent" / "project_state.yaml"
PHASE_STATUS_PATH = REPO_ROOT / ".agent" / "phase_status.yaml"
RUNTIME_PATH = REPO_ROOT / "config" / "ew0a_runtime.json"
CODEX_LOGIN = "chatgpt-codex-connector[bot]"
MATERIAL_FINDING_RE = re.compile(r"\\[P[0-3] Badge\\]")
REVIEWED_COMMIT_RE = re.compile(r"\\*\\*Reviewed commit:\\*\\*\\s*`([0-9a-fA-F]+)`")
MARKER_RE = re.compile(r"<!--\\s*northstar-orchestration\\s*(.*?)-->", re.S)


class OrchestrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DispatchState:
    mission_id: str
    phase: str


class GitHubClient:
    def __init__(self, repository: str, token: str):
        self.repository = repository
        self.token = token
        self.api = f"https://api.github.com/repos/{repository}"

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        url = path if path.startswith("https://") else f"{self.api}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "northstar-orchestrator")
        req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return None if not raw else json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise OrchestrationError(f"GitHub {method} {url} -> {exc.code}: {body}") from exc

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: Any) -> Any:
        return self.request("POST", path, payload)

    def put(self, path: str, payload: Any) -> Any:
        return self.request("PUT", path, payload)


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_registry() -> dict[str, Any]:
    data = load_yaml(REGISTRY_PATH)
    if data.get("schema_version") != "engineering.mission_registry.v1":
        raise OrchestrationError("unsupported mission registry schema")
    return data


def read_dispatch_state() -> DispatchState:
    project = load_yaml(PROJECT_STATE_PATH)
    phase = load_yaml(PHASE_STATUS_PATH)
    runtime = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
    mission = project.get("current_step")
    primary = (project.get("next_official_step") or {}).get("primary")
    phase_name = project.get("current_phase")
    ns = (phase.get("stockbot_northstar_redesign") or {})
    rt = ns.get("engineer_runtime_state") or {}
    phases = ns.get("phases") or {}
    active = phases.get(phase_name) or {}
    values = [mission, primary, rt.get("mission_id"), active.get("step"), runtime.get("mission_id")]
    if not mission or any(v != mission for v in values):
        raise OrchestrationError(f"dispatch mirrors disagree: {values}")
    return DispatchState(mission_id=str(mission), phase=str(phase_name))


def parse_marker(body: str | None) -> dict[str, str]:
    if not body:
        return {}
    match = MARKER_RE.search(body)
    if not match:
        return {}
    out: dict[str, str] = {}
    for raw in match.group(1).splitlines():
        raw = raw.strip()
        if not raw or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def marker_text(**fields: str) -> str:
    lines = ["<!-- northstar-orchestration"]
    lines.extend(f"{k}: {v}" for k, v in fields.items())
    lines.append("-->")
    return "\n".join(lines)


def latest_checks(check_runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for run in check_runs:
        name = run.get("name")
        if not name:
            continue
        previous = latest.get(name)
        if previous is None or int(run.get("id", 0)) > int(previous.get("id", 0)):
            latest[name] = run
    return latest


def reviewed_commit_from_body(body: str) -> str | None:
    match = REVIEWED_COMMIT_RE.search(body or "")
    return match.group(1).lower() if match else None


def codex_review_complete(
    head_sha: str,
    reviews: list[dict[str, Any]],
    issue_comments: list[dict[str, Any]],
) -> bool:
    for review in reviews:
        if review.get("user", {}).get("login") == CODEX_LOGIN and review.get("commit_id") == head_sha:
            return True
    for comment in issue_comments:
        if comment.get("user", {}).get("login") != CODEX_LOGIN:
            continue
        reviewed = reviewed_commit_from_body(comment.get("body") or "")
        if reviewed and head_sha.lower().startswith(reviewed):
            return True
    return False


def exact_head_material_findings(
    head_sha: str,
    review_comments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for comment in review_comments:
        if comment.get("user", {}).get("login") != CODEX_LOGIN:
            continue
        # GitHub carries comments forward and mutates commit_id. original_commit_id
        # is the stable evidence of which exact head the finding was created against.
        if comment.get("original_commit_id") != head_sha:
            continue
        if MATERIAL_FINDING_RE.search(comment.get("body") or ""):
            findings.append(comment)
    return findings


def review_already_requested(head_sha: str, issue_comments: list[dict[str, Any]]) -> bool:
    needle = head_sha.lower()
    for comment in issue_comments:
        body = (comment.get("body") or "").lower()
        if "@codex review" in body and needle in body:
            return True
    return False


def mission_entry(registry: dict[str, Any], mission_id: str) -> dict[str, Any]:
    entry = (registry.get("missions") or {}).get(mission_id)
    if not isinstance(entry, dict):
        raise OrchestrationError(f"mission {mission_id!r} not present in protected registry")
    return entry


def changed_paths(client: GitHubClient, pr_number: int) -> list[str]:
    files = client.get(f"/pulls/{pr_number}/files?per_page=100")
    return [str(item.get("filename")) for item in files]


def path_is_controller_protected(path: str, registry: dict[str, Any]) -> bool:
    for protected in (registry.get("orchestration") or {}).get("controller_protected_paths", []):
        if path == protected or path.startswith(protected.rstrip("/") + "/"):
            return True
    return False


def validate_marker_authority(
    marker: dict[str, str],
    state: DispatchState,
    registry: dict[str, Any],
) -> None:
    kind = marker.get("kind")
    if kind == "implementation":
        if marker.get("mission_id") != state.mission_id:
            raise OrchestrationError("implementation PR mission does not match protected current mission")
        return
    if kind == "governance_transition":
        if marker.get("transition_from") != state.mission_id:
            raise OrchestrationError("transition PR does not start from protected current mission")
        entry = mission_entry(registry, state.mission_id)
        transition = entry.get("on_success") or {}
        if transition.get("policy") != "preauthorized_auto":
            raise OrchestrationError("current mission does not authorize automatic transition")
        if marker.get("transition_to") != transition.get("next_mission"):
            raise OrchestrationError("transition target does not match protected mission registry")
        return
    raise OrchestrationError("missing or unsupported northstar orchestration PR marker")


def current_main_sha(client: GitHubClient) -> str:
    return str(client.get("/commits/main")["sha"])


def dispatch_ci(client: GitHubClient, ref: str) -> None:
    client.post("/actions/workflows/northstar-ci.yml/dispatches", {"ref": ref})


def request_codex_review(
    client: GitHubClient,
    pr_number: int,
    head_sha: str,
    issue_comments: list[dict[str, Any]],
) -> None:
    if review_already_requested(head_sha, issue_comments):
        return
    client.post(
        f"/issues/{pr_number}/comments",
        {"body": f"@codex review the exact head {head_sha} — Northstar controller exact-head certification request."},
    )


def refresh_stale_pr(client: GitHubClient, pr: dict[str, Any]) -> None:
    number = int(pr["number"])
    head = str(pr["head"]["sha"])
    client.put(f"/pulls/{number}/update-branch", {"expected_head_sha": head})
    refreshed = client.get(f"/pulls/{number}")
    for _ in range(10):
        if str(refreshed["head"]["sha"]) != head:
            break
        time.sleep(1)
        refreshed = client.get(f"/pulls/{number}")
    new_head = str(refreshed["head"]["sha"])
    if new_head == head:
        raise OrchestrationError("GitHub accepted update-branch but the PR head did not advance")
    dispatch_ci(client, str(refreshed["head"]["ref"]))
    comments = client.get(f"/issues/{number}/comments?per_page=100")
    request_codex_review(client, number, new_head, comments)


def evaluate_pr(
    client: GitHubClient,
    pr_number: int,
    registry: dict[str, Any],
) -> str:
    state = read_dispatch_state()
    pr = client.get(f"/pulls/{pr_number}")
    if pr.get("state") != "open" or pr.get("merged"):
        return "not_open"

    marker = parse_marker(pr.get("body"))
    if not marker:
        return "unmanaged_pr"
    validate_marker_authority(marker, state, registry)

    kind = marker["kind"]
    files = changed_paths(client, pr_number)
    orchestration_mission = (registry.get("orchestration") or {}).get("mission_id")
    if kind == "implementation" and state.mission_id != orchestration_mission:
        protected = [path for path in files if path_is_controller_protected(path, registry)]
        if protected:
            raise OrchestrationError(
                f"ordinary mission PR modifies controller-protected paths: {protected}"
            )

    main_sha = current_main_sha(client)
    if pr.get("base", {}).get("sha") != main_sha:
        refresh_stale_pr(client, pr)
        return "base_refreshed"

    head_sha = str(pr["head"]["sha"])
    check_runs = client.get(f"/commits/{head_sha}/check-runs?per_page=100").get("check_runs", [])
    checks = latest_checks(check_runs)
    required = list((registry.get("orchestration") or {}).get("required_check_names", []))
    for name in required:
        run = checks.get(name)
        if not run or run.get("status") != "completed" or run.get("conclusion") != "success":
            return "awaiting_ci"

    reviews = client.get(f"/pulls/{pr_number}/reviews?per_page=100")
    review_comments = client.get(f"/pulls/{pr_number}/comments?per_page=100")
    issue_comments = client.get(f"/issues/{pr_number}/comments?per_page=100")

    findings = exact_head_material_findings(head_sha, review_comments)
    if findings:
        return "codex_findings"

    if not codex_review_complete(head_sha, reviews, issue_comments):
        request_codex_review(client, pr_number, head_sha, issue_comments)
        return "awaiting_codex"

    merged = client.put(
        f"/pulls/{pr_number}/merge",
        {"merge_method": "merge", "sha": head_sha},
    )
    if not merged.get("merged"):
        raise OrchestrationError(f"GitHub refused exact-head merge: {merged}")

    # GITHUB_TOKEN-generated merges do not reliably recurse into push workflows.
    # Explicit dispatch makes post-merge certification deterministic.
    dispatch_ci(client, "main")
    return "merged"


def associated_prs(client: GitHubClient, sha: str) -> list[dict[str, Any]]:
    try:
        return client.get(f"/commits/{sha}/pulls?per_page=100")
    except OrchestrationError:
        return []


def find_open_mission_pr(client: GitHubClient, mission_id: str) -> int | None:
    for pr in client.get("/pulls?state=open&base=main&per_page=100"):
        marker = parse_marker(pr.get("body"))
        if marker.get("kind") == "implementation" and marker.get("mission_id") == mission_id:
            return int(pr["number"])
    return None


def dispatch_claude(
    client: GitHubClient,
    mission_id: str,
    main_sha: str,
) -> None:
    client.post(
        "/dispatches",
        {
            "event_type": "northstar-claude-dispatch",
            "client_payload": {
                "mission_id": mission_id,
                "starting_main_sha": main_sha,
                "mode": "implementation",
            },
        },
    )


def open_human_gate_issue(
    client: GitHubClient,
    current: str,
    next_mission: str | None,
) -> None:
    title = f"Northstar approval required after {current}"
    for issue in client.get("/issues?state=open&per_page=100"):
        if issue.get("title") == title:
            return
    client.post(
        "/issues",
        {
            "title": title,
            "body": (
                f"Mission `{current}` is durable and post-merge certified. "
                f"The registry marks the next transition (`{next_mission or 'unspecified'}`) "
                "as `human_required`. No state transition or Claude dispatch was performed."
            ),
        },
    )


def create_transition_pr(
    client: GitHubClient,
    registry: dict[str, Any],
    current: str,
    next_mission: str,
    completion_sha: str,
) -> int:
    entry = mission_entry(registry, current)
    transition = entry.get("on_success") or {}
    if transition.get("policy") != "preauthorized_auto":
        raise OrchestrationError("transition is not preauthorized")
    if transition.get("next_mission") != next_mission:
        raise OrchestrationError("transition target drift")

    branch = f"governance/auto-transition-{current}-to-{next_mission}-{completion_sha[:8]}"
    subprocess.run(
        ["git", "config", "user.email", "northstar-controller@users.noreply.github.com"],
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "northstar-controller"], check=True)
    subprocess.run(["git", "checkout", "-b", branch], check=True)
    subprocess.run(
        [
            sys.executable,
            "scripts/northstar_transition.py",
            "apply",
            "--from-mission",
            current,
            "--to-mission",
            next_mission,
            "--completion-sha",
            completion_sha,
        ],
        check=True,
    )
    subprocess.run(["git", "add", "-A"], check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        raise OrchestrationError("transition recipe produced no changes")
    subprocess.run(
        ["git", "commit", "-m", f"governance: transition {current} to {next_mission}"],
        check=True,
    )
    subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], check=True)
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    body = marker_text(
        kind="governance_transition",
        transition_from=current,
        transition_to=next_mission,
        completion_sha=completion_sha,
    ) + "\n\nMachine-generated governance transition from the protected mission registry."

    pr = client.post(
        "/pulls",
        {
            "title": f"governance: transition {current} -> {next_mission}",
            "head": branch,
            "base": "main",
            "body": body,
        },
    )
    number = int(pr["number"])
    dispatch_ci(client, branch)
    client.post(
        f"/issues/{number}/comments",
        {"body": f"@codex review the exact head {head_sha} — machine-generated protected-state transition."},
    )
    return number


def handoff_mission(
    client: GitHubClient,
    registry: dict[str, Any],
    mission_id: str,
    main_sha: str,
) -> str:
    entry = mission_entry(registry, mission_id)
    existing = find_open_mission_pr(client, mission_id)
    if existing is not None:
        return evaluate_pr(client, existing, registry)
    if entry.get("auto_dispatch") is True and entry.get("executor") == "claude":
        dispatch_claude(client, mission_id, main_sha)
        return "claude_dispatched"
    return "handoff_not_automatic"


def handle_main_certification(
    client: GitHubClient,
    registry: dict[str, Any],
    sha: str,
) -> str:
    state = read_dispatch_state()
    prs = [pr for pr in associated_prs(client, sha) if pr.get("merged_at")]
    marker_pr: tuple[dict[str, Any], dict[str, str]] | None = None
    for pr in prs:
        marker = parse_marker(pr.get("body"))
        if marker:
            marker_pr = (pr, marker)
            break

    if marker_pr is None:
        return "certified_main_without_orchestration_marker"

    _pr, marker = marker_pr
    kind = marker.get("kind")

    if kind == "governance_transition":
        if marker.get("transition_to") != state.mission_id:
            raise OrchestrationError(
                "merged transition target does not match durable protected state"
            )
        return handoff_mission(client, registry, state.mission_id, sha)

    if kind != "implementation" or marker.get("mission_id") != state.mission_id:
        return "certified_noncurrent_mission"

    entry = mission_entry(registry, state.mission_id)
    transition = entry.get("on_success") or {}
    policy = transition.get("policy", "human_required")
    next_mission = transition.get("next_mission")

    if policy == "preauthorized_auto":
        if not next_mission:
            raise OrchestrationError("preauthorized_auto transition missing next_mission")
        create_transition_pr(
            client,
            registry,
            state.mission_id,
            str(next_mission),
            sha,
        )
        return "transition_pr_created"

    open_human_gate_issue(
        client,
        state.mission_id,
        str(next_mission) if next_mission else None,
    )
    return "human_gate_created"


def handle_event(
    client: GitHubClient,
    registry: dict[str, Any],
    event: dict[str, Any],
) -> str:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")

    if event_name == "workflow_run":
        run = event.get("workflow_run") or {}
        if run.get("name") != "northstar-ci" or run.get("status") != "completed":
            return "ignored_workflow_run"

        sha = str(run.get("head_sha") or "")
        if not sha:
            return "workflow_run_missing_sha"

        if run.get("conclusion") != "success":
            for pr in associated_prs(client, sha):
                if pr.get("state") == "open":
                    return "ci_failed"
            return "failed_main_ci"

        for pr in associated_prs(client, sha):
            if pr.get("state") == "open":
                return evaluate_pr(client, int(pr["number"]), registry)

        if run.get("head_branch") == "main" and sha == current_main_sha(client):
            return handle_main_certification(client, registry, sha)

        return "certified_nonmain_without_open_pr"

    if event_name in {
        "pull_request_review",
        "pull_request_review_comment",
        "issue_comment",
    }:
        pr = event.get("pull_request") or event.get("issue") or {}
        number = pr.get("number")
        if not number or not (pr.get("pull_request") or event.get("pull_request")):
            return "ignored_non_pr_comment"
        return evaluate_pr(client, int(number), registry)

    if event_name == "workflow_dispatch":
        state = read_dispatch_state()
        open_pr = find_open_mission_pr(client, state.mission_id)
        if open_pr is not None:
            return evaluate_pr(client, open_pr, registry)
        return handoff_mission(
            client,
            registry,
            state.mission_id,
            current_main_sha(client),
        )

    return f"ignored_event:{event_name}"


def open_claude_pr(
    client: GitHubClient,
    registry: dict[str, Any],
    branch: str,
    mission_id: str,
    starting_main_sha: str,
) -> int:
    state = read_dispatch_state()
    if state.mission_id != mission_id:
        raise OrchestrationError(
            "Claude output mission no longer matches protected current mission"
        )

    mission_entry(registry, mission_id)
    branch_info = client.get(f"/branches/{urllib.parse.quote(branch, safe='')}")
    head_sha = str(branch_info["commit"]["sha"])

    body = marker_text(
        kind="implementation",
        mission_id=mission_id,
        starting_main_sha=starting_main_sha,
    ) + "\n\nCreated by the protected Northstar Claude handoff workflow."

    pr = client.post(
        "/pulls",
        {
            "title": mission_id,
            "head": branch,
            "base": "main",
            "body": body,
        },
    )
    number = int(pr["number"])
    dispatch_ci(client, branch)
    client.post(
        f"/issues/{number}/comments",
        {"body": f"@codex review the exact head {head_sha} — Northstar controller exact-head certification request."},
    )
    return number


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    event_parser = sub.add_parser("event")
    event_parser.add_argument("--event-path", required=True)

    pr_parser = sub.add_parser("evaluate-pr")
    pr_parser.add_argument("--pr", type=int, required=True)

    open_parser = sub.add_parser("open-claude-pr")
    open_parser.add_argument("--branch", required=True)
    open_parser.add_argument("--mission", required=True)
    open_parser.add_argument("--starting-main-sha", required=True)

    args = parser.parse_args()

    repository = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repository or not token:
        raise OrchestrationError("GITHUB_REPOSITORY and GITHUB_TOKEN are required")

    client = GitHubClient(repository, token)
    registry = load_registry()

    if args.command == "event":
        event = json.loads(Path(args.event_path).read_text(encoding="utf-8"))
        result = handle_event(client, registry, event)
    elif args.command == "evaluate-pr":
        result = evaluate_pr(client, args.pr, registry)
    else:
        result = str(
            open_claude_pr(
                client,
                registry,
                args.branch,
                args.mission,
                args.starting_main_sha,
            )
        )

    print(f"NORTHSTAR_ORCHESTRATOR_RESULT={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
