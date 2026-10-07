#!/usr/bin/env python3
"""GitLab REST helpers for SDLC OpenCode agents (PAT / PRIVATE-TOKEN).

Replaces GitLab MCP for Demo A on GitLab versions without /api/v4/mcp.

Env:
  GITLAB_URL   — base URL (https://gitlab.example.com)
  GITLAB_PAT   — preferred token (PRIVATE-TOKEN)
  GITLAB_TOKEN — alias for GITLAB_PAT
  GITLAB_USERNAME / GITLAB_PASSWORD — optional; used only if no PAT (session login)

Examples:
  python3 /app/scripts/gitlab_api.py project-get --path lightwell/lw-demo-help-app-GUID
  python3 /app/scripts/gitlab_api.py mr-get --project-id 1 --mr-iid 2
  python3 /app/scripts/gitlab_api.py mr-note --project-id 1 --mr-iid 2 --body "verify ok"
  python3 /app/scripts/gitlab_api.py bump-maven-mr \\
    --path lightwell/lw-demo-help-app-GUID \\
    --artifact-id org.json:json \\
    --new-version 20220320.0.0.rhlw-00003 \\
    --impact-text "Bump remediates CVE-2022-45688 for demo A."
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def env_url() -> str:
    url = (os.environ.get("GITLAB_URL") or "").rstrip("/")
    if not url:
        die("GITLAB_URL is required")
    return url


def env_token() -> str:
    token = os.environ.get("GITLAB_PAT") or os.environ.get("GITLAB_TOKEN") or ""
    if token:
        return token
    # Optional password-as-token (some demos store root PAT elsewhere only)
    user = os.environ.get("GITLAB_USERNAME") or ""
    password = os.environ.get("GITLAB_PASSWORD") or ""
    if user and password:
        # Prefer explicit PAT; password login via session cookie is not implemented.
        die("GITLAB_PAT (or GITLAB_TOKEN) is required; username/password alone is not enough for API writes")
    die("GITLAB_PAT (or GITLAB_TOKEN) is required")


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


class GitlabHTTPError(RuntimeError):
    def __init__(self, method: str, path: str, code: int, body: str):
        super().__init__(f"{method} {path} → HTTP {code}: {body[:800]}")
        self.code = code
        self.body = body


def api(
    method: str,
    path: str,
    body: dict | list | None = None,
    *,
    raw: bool = False,
) -> Any:
    base = env_url()
    token = env_token()
    url = f"{base}{path}" if path.startswith("/api/") else f"{base}/api/v4{path}"
    data = None
    headers = {
        "PRIVATE-TOKEN": token,
        "Accept": "application/json",
        "User-Agent": "lw-sdlc-opencode-gitlab-api/1.0",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            payload = resp.read()
            if raw:
                return payload.decode("utf-8", errors="replace")
            if not payload.strip():
                return None
            return json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise GitlabHTTPError(method, path, e.code, err_body) from e


def api_or_die(
    method: str,
    path: str,
    body: dict | list | None = None,
    *,
    raw: bool = False,
) -> Any:
    try:
        return api(method, path, body, raw=raw)
    except GitlabHTTPError as e:
        die(str(e))


def out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def sanitize_branch_version(version: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", version.strip())
    return cleaned.strip("-") or "unknown"


def parse_artifact_id(artifact_id: str) -> tuple[str, str]:
    """Return (group_id, artifact_id) from Maven coordinate group:artifact."""
    parts = artifact_id.strip().split(":")
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return "", parts[0]
    # group:artifact:version — ignore version
    if len(parts) >= 2:
        return parts[0], parts[1]
    die(f"invalid artifact_id: {artifact_id!r} (expected group:artifact)")


def bump_maven_dependency(pom: str, artifact_id: str, new_version: str) -> tuple[str, str]:
    """Replace <version> inside the dependency block matching group+artifact.

    Returns (new_pom, old_version_or_empty).
    """
    group_id, art_id = parse_artifact_id(artifact_id)
    if not group_id:
        die(f"artifact_id must include groupId (got {artifact_id!r})")

    # Match a dependency block containing both groupId and artifactId.
    pattern = re.compile(
        r"(<dependency>\s*"
        r"<groupId>\s*" + re.escape(group_id) + r"\s*</groupId>\s*"
        r"<artifactId>\s*" + re.escape(art_id) + r"\s*</artifactId>\s*)"
        r"<version>\s*([^<]+?)\s*</version>",
        re.DOTALL,
    )
    m = pattern.search(pom)
    if not m:
        # Alternate order: artifactId before groupId (rare)
        pattern2 = re.compile(
            r"(<dependency>\s*"
            r"<artifactId>\s*" + re.escape(art_id) + r"\s*</artifactId>\s*"
            r"<groupId>\s*" + re.escape(group_id) + r"\s*</groupId>\s*)"
            r"<version>\s*([^<]+?)\s*</version>",
            re.DOTALL,
        )
        m = pattern2.search(pom)
        if not m:
            die(f"dependency {group_id}:{art_id} not found in pom.xml")
        old = m.group(2).strip()
        new_pom = pattern2.sub(rf"\1<version>{new_version}</version>", pom, count=1)
        return new_pom, old

    old = m.group(2).strip()
    new_pom = pattern.sub(rf"\1<version>{new_version}</version>", pom, count=1)
    return new_pom, old


DEMO_STATUS_PATH = "src/main/java/com/redhat/tpa/vulnerable/HelloController.java"
DEMO_STATUS_NAMES = {
    "woodstox-core": "Woodstox",
    "json": "org.json",
    "json-path": "json-path",
    "httpclient": "Apache HttpClient",
    "json-smart": "json-smart",
    "commons-fileupload": "Commons FileUpload",
    "commons-io": "Commons IO",
    "h2": "H2 Database",
    "plexus-utils": "plexus-utils",
    "hutool-json": "hutool-json",
}


def update_demo_status(controller: str, artifact_id: str, new_version: str) -> str:
    """Keep the demo app's /api/status remediation marker in sync with its POM."""
    group_id, art_id = parse_artifact_id(artifact_id)
    name = DEMO_STATUS_NAMES.get(art_id)
    if not name:
        return controller
    # The version field documents the vulnerable baseline; lightwellFix is the
    # remediated version that the smoke test checks after the Maven bump.
    pattern = re.compile(
        r'(results\.add\(check\("' + re.escape(name) + r'",\s*"'
        + re.escape(group_id) + r'",\s*"[^"]+",\s*"[^"]*",\s*")'
        + r'([^"]*)(")'
    )
    matches = list(pattern.finditer(controller))
    if len(matches) != 1:
        die(f"expected one /api/status row for {artifact_id}, found {len(matches)}")
    match = matches[0]
    return controller[:match.start(2)] + new_version + controller[match.end(2):]


def resolve_project(path: str | None, project_id: int | None) -> dict:
    if project_id:
        return api_or_die("GET", f"/projects/{project_id}")
    if not path:
        die("--path or --project-id is required")
    enc = urllib.parse.quote(path, safe="")
    return api_or_die("GET", f"/projects/{enc}")


def cmd_project_get(args: argparse.Namespace) -> None:
    out(resolve_project(args.path, args.project_id))


def cmd_file_get(args: argparse.Namespace) -> None:
    project = resolve_project(args.path, args.project_id)
    pid = project["id"]
    enc = urllib.parse.quote(args.file_path, safe="")
    ref = urllib.parse.quote(args.ref, safe="")
    raw = api_or_die("GET", f"/projects/{pid}/repository/files/{enc}/raw?ref={ref}", raw=True)
    if args.json:
        out({"project_id": pid, "file_path": args.file_path, "ref": args.ref, "content": raw})
    else:
        sys.stdout.write(raw)


def cmd_branch_create(args: argparse.Namespace) -> None:
    project = resolve_project(args.path, args.project_id)
    pid = project["id"]
    try:
        created = api(
            "POST",
            f"/projects/{pid}/repository/branches",
            {"branch": args.branch, "ref": args.ref},
        )
        out(created)
        return
    except GitlabHTTPError as e:
        if e.code not in (400, 409):
            die(str(e))
    existing = api_or_die(
        "GET",
        f"/projects/{pid}/repository/branches/{urllib.parse.quote(args.branch, safe='')}",
    )
    out(existing)


def cmd_commit_file(args: argparse.Namespace) -> None:
    project = resolve_project(args.path, args.project_id)
    pid = project["id"]
    content = args.content
    if args.content_file:
        content = open(args.content_file, encoding="utf-8").read()
    if content is None:
        die("--content or --content-file required")
    body: dict[str, Any] = {
        "branch": args.branch,
        "commit_message": args.message,
        "actions": [
            {
                "action": args.action,
                "file_path": args.file_path,
                "content": content,
            }
        ],
    }
    if args.start_branch:
        body["start_branch"] = args.start_branch
    out(api_or_die("POST", f"/projects/{pid}/repository/commits", body))


def build_mr_description(
    artifact_id: str,
    new_version: str,
    gitlab_path: str,
    impact_text: str,
    handoff: dict,
) -> str:
    handoff_json = json.dumps(handoff, indent=2)
    return (
        f"## Dependency Update\n"
        f"Artifact: `{artifact_id}`\n"
        f"New Version: `{new_version}`\n"
        f"GitLab path: `{gitlab_path}`\n"
        f"Blast radius: demo A (single app)\n\n"
        f"## Impact Analysis\n"
        f"{impact_text.strip() or '_No analysis provided._'}\n\n"
        f"<!-- agent-handoff: do not edit below -->\n"
        f"```json\n{handoff_json}\n```\n"
    )


def cmd_mr_create(args: argparse.Namespace) -> None:
    project = resolve_project(args.path, args.project_id)
    pid = project["id"]
    body = {
        "source_branch": args.source_branch,
        "target_branch": args.target_branch,
        "title": args.title,
        "description": args.description or "",
        "remove_source_branch": False,
    }
    out(api_or_die("POST", f"/projects/{pid}/merge_requests", body))


def cmd_mr_get(args: argparse.Namespace) -> None:
    project = resolve_project(args.path, args.project_id)
    pid = project["id"]
    out(api_or_die("GET", f"/projects/{pid}/merge_requests/{args.mr_iid}"))


def cmd_mr_note(args: argparse.Namespace) -> None:
    project = resolve_project(args.path, args.project_id)
    pid = project["id"]
    out(
        api_or_die(
            "POST",
            f"/projects/{pid}/merge_requests/{args.mr_iid}/notes",
            {"body": args.body},
        )
    )


def cmd_bump_maven_mr(args: argparse.Namespace) -> None:
    project = resolve_project(args.path, args.project_id)
    pid = int(project["id"])
    path_with_ns = project.get("path_with_namespace") or args.path or ""
    default_branch = args.target_branch or project.get("default_branch") or "main"
    branch = args.branch or f"update-artifact-{sanitize_branch_version(args.new_version)}"

    enc = urllib.parse.quote(args.pom_path, safe="")
    ref = urllib.parse.quote(default_branch, safe="")
    pom = api_or_die("GET", f"/projects/{pid}/repository/files/{enc}/raw?ref={ref}", raw=True)
    new_pom, old_version = bump_maven_dependency(pom, args.artifact_id, args.new_version)
    if new_pom == pom:
        die(f"pom unchanged (already at {args.new_version}?)")

    status_update = None
    status_path = urllib.parse.quote(DEMO_STATUS_PATH, safe="")
    try:
        controller = api("GET", f"/projects/{pid}/repository/files/{status_path}/raw?ref={ref}", raw=True)
    except GitlabHTTPError as e:
        if e.code != 404:
            die(str(e))
    else:
        updated_controller = update_demo_status(controller, args.artifact_id, args.new_version)
        if updated_controller != controller:
            status_update = {
                "action": "update",
                "file_path": DEMO_STATUS_PATH,
                "content": updated_controller,
            }

    branch_enc = urllib.parse.quote(branch, safe="")
    try:
        api("GET", f"/projects/{pid}/repository/branches/{branch_enc}")
        branch_exists = True
    except GitlabHTTPError as e:
        if e.code != 404:
            die(str(e))
        branch_exists = False

    if not branch_exists:
        api_or_die(
            "POST",
            f"/projects/{pid}/repository/branches",
            {"branch": branch, "ref": default_branch},
        )

    commit_msg = f"chore(deps): update {args.artifact_id} to {args.new_version}"
    actions = [{"action": "update", "file_path": args.pom_path, "content": new_pom}]
    if status_update:
        actions.append(status_update)
    api_or_die(
        "POST",
        f"/projects/{pid}/repository/commits",
        {
            "branch": branch,
            "commit_message": commit_msg,
            "actions": actions,
        },
    )

    mrs = api_or_die(
        "GET",
        f"/projects/{pid}/merge_requests?state=opened&source_branch={urllib.parse.quote(branch)}",
    )
    if isinstance(mrs, list) and mrs:
        mr = mrs[0]
    else:
        handoff = {
            "artifact_id": args.artifact_id,
            "new_version": args.new_version,
            "old_version": old_version,
            "source_branch": branch,
            "target_branch": default_branch,
            "project_id": pid,
            "merge_request_iid": 0,
            "gitlab_path": path_with_ns,
        }
        title = f"chore(deps): update {args.artifact_id} to {args.new_version}"
        description = build_mr_description(
            args.artifact_id,
            args.new_version,
            path_with_ns,
            args.impact_text or "",
            handoff,
        )
        mr = api_or_die(
            "POST",
            f"/projects/{pid}/merge_requests",
            {
                "source_branch": branch,
                "target_branch": default_branch,
                "title": title,
                "description": description,
            },
        )
        iid = int(mr["iid"])
        handoff["merge_request_iid"] = iid
        description = build_mr_description(
            args.artifact_id,
            args.new_version,
            path_with_ns,
            args.impact_text or "",
            handoff,
        )
        mr = api_or_die(
            "PUT",
            f"/projects/{pid}/merge_requests/{iid}",
            {"description": description},
        )

    out(
        {
            "project_id": pid,
            "gitlab_path": path_with_ns,
            "branch": branch,
            "old_version": old_version,
            "new_version": args.new_version,
            "artifact_id": args.artifact_id,
            "merge_request_iid": mr.get("iid"),
            "merge_request_url": mr.get("web_url"),
            "title": mr.get("title"),
        }
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="GitLab REST CLI for SDLC OpenCode agents")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_project_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--path", help="project path_with_namespace")
        sp.add_argument("--project-id", type=int, help="numeric project id")

    sp = sub.add_parser("project-get", help="Resolve a GitLab project")
    add_project_flags(sp)
    sp.set_defaults(func=cmd_project_get)

    sp = sub.add_parser("file-get", help="Get a repository file (raw)")
    add_project_flags(sp)
    sp.add_argument("--file-path", required=True)
    sp.add_argument("--ref", default="main")
    sp.add_argument("--json", action="store_true", help="wrap content in JSON")
    sp.set_defaults(func=cmd_file_get)

    sp = sub.add_parser("branch-create", help="Create a branch (idempotent)")
    add_project_flags(sp)
    sp.add_argument("--branch", required=True)
    sp.add_argument("--ref", default="main", help="source ref")
    sp.set_defaults(func=cmd_branch_create)

    sp = sub.add_parser("commit-file", help="Commit a file create/update on a branch")
    add_project_flags(sp)
    sp.add_argument("--branch", required=True)
    sp.add_argument("--start-branch", default="", help="create branch from this if missing")
    sp.add_argument("--file-path", required=True)
    sp.add_argument("--message", required=True)
    sp.add_argument("--action", default="update", choices=["create", "update", "delete"])
    sp.add_argument("--content", default=None)
    sp.add_argument("--content-file", default=None)
    sp.set_defaults(func=cmd_commit_file)

    sp = sub.add_parser("mr-create", help="Create a merge request")
    add_project_flags(sp)
    sp.add_argument("--source-branch", required=True)
    sp.add_argument("--target-branch", default="main")
    sp.add_argument("--title", required=True)
    sp.add_argument("--description", default="")
    sp.set_defaults(func=cmd_mr_create)

    sp = sub.add_parser("mr-get", help="Get a merge request")
    add_project_flags(sp)
    sp.add_argument("--mr-iid", type=int, required=True)
    sp.set_defaults(func=cmd_mr_get)

    sp = sub.add_parser("mr-note", help="Post a note on a merge request")
    add_project_flags(sp)
    sp.add_argument("--mr-iid", type=int, required=True)
    sp.add_argument("--body", required=True)
    sp.set_defaults(func=cmd_mr_note)

    sp = sub.add_parser(
        "bump-maven-mr",
        help="Demo A: bump Maven dependency in pom.xml, push branch, open MR with handoff JSON",
    )
    add_project_flags(sp)
    sp.add_argument("--artifact-id", required=True, help="group:artifact")
    sp.add_argument("--new-version", required=True)
    sp.add_argument("--pom-path", default="pom.xml")
    sp.add_argument("--target-branch", default="")
    sp.add_argument("--branch", default="", help="default update-artifact-<version>")
    sp.add_argument("--impact-text", default="", help="Impact Analysis paragraph for MR body")
    sp.set_defaults(func=cmd_bump_maven_mr)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
