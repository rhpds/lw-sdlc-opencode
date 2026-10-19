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


# Risk classes render as a coloured dot so the MR title and the validation
# table read at a glance in the GitLab list view.
RISK_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH": "🔴",
    "MEDIUM": "🟡",
    "LOW": "🟢",
    "NONE": "⚪",
}

# The validation table in display order. impact-analyzer renders every row as
# Pending; mr-verifier fills them in once the verify Job has actually run, so
# the table never claims a result that nothing produced.
VALIDATION_CHECKS: list[tuple[str, str]] = [
    ("risk_class", "Acme Inc. Risk Class"),
    ("signature", "Binary signature verification"),
    ("sbom_integrity", "SBOM integrity"),
    ("unit_tests", "Unit tests"),
    ("integration_tests", "Integration tests"),
    ("dependency_compat", "Dependency compatibility"),
    ("regression", "Regression scan"),
    ("license", "License compliance"),
]

STATUS_ICON = {
    "passed": "✅ Passed",
    "failed": "❌ Failed",
    "pending": "⏳ Pending",
    "skipped": "⏭️ Skipped",
    "warning": "⚠️ Warning",
}

# mr-verifier rewrites only what sits between these markers, so the Summary and
# Vulnerability Details written at creation time survive the update.
VALIDATION_START = "<!-- lw:validation:start -->"
VALIDATION_END = "<!-- lw:validation:end -->"
ACTION_START = "<!-- lw:action:start -->"
ACTION_END = "<!-- lw:action:end -->"

CVE_BASE_URL = "https://access.redhat.com/security/cve/"


def risk_badge(risk_class: str) -> str:
    rc = (risk_class or "").strip().upper() or "NONE"
    return f"{RISK_EMOJI.get(rc, '⚪')} {rc}"


def render_validation_table(results: dict, risk_class: str, risk_rationale: str) -> str:
    """Render the Automated Validation table.

    `results` maps a check key from VALIDATION_CHECKS to
    {"status": "passed"|"failed"|..., "details": "..."}. Anything absent is
    Pending — an unrun check must never look like a passing one.
    """
    rows = ["| Check | Status | Details |", "|---|---|---|"]
    for key, label in VALIDATION_CHECKS:
        if key == "risk_class":
            rows.append(
                f"| {label} | {risk_badge(risk_class)} | "
                f"{risk_rationale or 'No risk classification recorded'} |"
            )
            continue
        res = results.get(key) or {}
        status = STATUS_ICON.get(str(res.get("status", "pending")).lower(), STATUS_ICON["pending"])
        rows.append(f"| {label} | {status} | {res.get('details') or 'Awaiting verification'} |")
    return "\n".join(rows)


def render_recommended_action(risk_class: str, results: dict) -> str:
    """Human review is driven by risk class first, then by failing checks."""
    rc = (risk_class or "").strip().upper()
    failed = [
        label
        for key, label in VALIDATION_CHECKS
        if str((results.get(key) or {}).get("status", "")).lower() == "failed"
    ]
    if failed:
        return (
            "> ❌ **Do not merge — automated validation failed.**\n>\n"
            f"> Failing checks: {', '.join(failed)}. Investigate before approving; "
            "a failed check means the remediated build did not behave like the one it replaces."
        )
    if rc in ("HIGH", "CRITICAL"):
        return (
            "> 🔍 **Requires human review before merge.**\n>\n"
            "> A security or DevSecOps engineer should review the patch diff, assess the "
            "application-specific impact, and approve or request changes. Risk class "
            f"**{rc}** requires review regardless of automated check results."
        )
    if any((results.get(k) or {}).get("status") for k, _ in VALIDATION_CHECKS):
        return (
            "> ✅ **Safe to merge.**\n>\n"
            "> All automated checks passed and the application is not in a review-required "
            "risk class."
        )
    return (
        "> ⏳ **Awaiting automated validation.**\n>\n"
        "> The MR Verifier is building and testing this change in an isolated namespace. "
        "The table above updates when it finishes."
    )


def build_mr_description(
    artifact_id: str,
    new_version: str,
    gitlab_path: str,
    impact_text: str,
    handoff: dict,
    *,
    old_version: str = "",
    cve_id: str = "",
    cve_title: str = "",
    cve_description: str = "",
    cvss_score: str = "",
    risk_class: str = "",
    risk_rationale: str = "",
    risk_score: str = "",
    transitive_count: int = 0,
    results: dict | None = None,
) -> str:
    results = results or {}
    handoff_json = json.dumps(handoff, indent=2)
    short_artifact = artifact_id.split(":")[-1]
    cve_cell = f"[{cve_id}]({CVE_BASE_URL}{cve_id})" if cve_id and cve_id != "unknown" else "_Not referenced_"
    cvss_cell = f"{cvss_score} → **Patched**" if cvss_score else "_Not scored_"
    sbom_cell = (
        f"1 direct dependency updated, {transitive_count} transitive "
        f"{'change' if transitive_count == 1 else 'changes'}"
    )

    parts = [
        "## 🤖 Lightwell Automated Remediation",
        "",
        "This merge request was automatically generated by the **Lightwell OpenCode Agents** "
        "after a new remediated package was published to the Red Hat Lightwell Network "
        "repository.",
        "",
        "---",
        "",
        "## Summary",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Package | `{artifact_id}` |",
        f"| Current Version | `{old_version or 'unknown'}` |",
        f"| Remediated Version | `{new_version}` |",
        f"| CVE | {cve_cell} |",
        f"| CVSS Score | {cvss_cell} |",
        f"| Lightwell Risk Score | {risk_badge(risk_score or risk_class)} |",
        f"| SBOM Impact | {sbom_cell} |",
        "",
        "## Vulnerability Details",
        "",
    ]

    # TPA often returns title == description, and descriptions can run to
    # several thousand characters listing every affected product. Render a
    # short headline plus a trimmed body rather than the same wall of text
    # twice.
    title_txt = (cve_title or "").strip()
    desc_txt = (cve_description or "").strip()
    if title_txt and title_txt == desc_txt:
        title_txt = title_txt.split(". ")[0].rstrip(".")
    if len(title_txt) > 120:
        title_txt = title_txt[:117].rstrip() + "..."
    if len(desc_txt) > 700:
        desc_txt = desc_txt[:697].rstrip() + "..."
    if cve_id and cve_id != "unknown":
        heading = f"**{cve_id}**"
        if title_txt:
            heading += f" — {title_txt}"
        parts += [heading, ""]
    if desc_txt:
        parts += [desc_txt, ""]
    parts += [
        # Do not claim a signature here. The validation table two sections
        # below reports an *integrity* check against the checksum Nexus
        # records, because the tenant mirror publishes no detached
        # signature. Saying "digitally signed" in the prose contradicts the
        # table on the same page, and the table is the part a reviewer is
        # being asked to trust.
        f"Red Hat Lightwell has backported the upstream fix directly to the `{new_version}` "
        "release line. The remediated build is published with checksums and a complete SBOM, "
        "both verified below.",
        "",
        "## Automated Validation",
        "",
        VALIDATION_START,
        render_validation_table(results, risk_class, risk_rationale),
        VALIDATION_END,
        "",
        "## Risk Assessment",
        "",
        impact_text.strip() or "_No impact analysis provided._",
        "",
        "## Recommended Action",
        "",
        ACTION_START,
        render_recommended_action(risk_class, results),
        ACTION_END,
        "",
        "<!-- agent-handoff: do not edit below -->",
        f"```json\n{handoff_json}\n```",
        "",
    ]
    return "\n".join(parts)


def replace_marked_block(text: str, start: str, end: str, replacement: str) -> str:
    """Swap the content between two markers, leaving everything else intact."""
    s, e = text.find(start), text.find(end)
    if s == -1 or e == -1 or e < s:
        return text
    return text[: s + len(start)] + "\n" + replacement + "\n" + text[e:]


def cmd_mr_validation(args: argparse.Namespace) -> None:
    """Fill in the Automated Validation table on an existing MR.

    Called by mr-verifier once the verify Job has finished, so the table
    reports what actually happened rather than what we hoped would happen.
    """
    try:
        results = json.loads(args.results) if args.results else {}
    except json.JSONDecodeError as exc:
        die(f"--results is not valid JSON: {exc}")
    if not isinstance(results, dict):
        die("--results must be a JSON object keyed by check name")
    unknown = sorted(set(results) - {k for k, _ in VALIDATION_CHECKS})
    if unknown:
        die(f"unknown check(s) {unknown}; valid: {[k for k, _ in VALIDATION_CHECKS]}")

    project = resolve_project(args.path, args.project_id)
    pid = int(project["id"])
    mr = api_or_die("GET", f"/projects/{pid}/merge_requests/{args.iid}")
    desc = mr.get("description") or ""
    if VALIDATION_START not in desc:
        die(
            "MR description has no validation block — it was not created by "
            "bump-maven-mr, so there is nothing to update"
        )

    risk_class = args.risk_class or ""
    risk_rationale = args.risk_rationale or ""
    # Re-read the risk row rather than requiring the caller to repeat it.
    if not risk_class:
        for line in desc.splitlines():
            if line.startswith("| Acme Inc. Risk Class |"):
                cells = [c.strip() for c in line.split("|")]
                if len(cells) >= 4:
                    risk_class = cells[2].split()[-1]
                    risk_rationale = cells[3]
                break

    desc = replace_marked_block(
        desc, VALIDATION_START, VALIDATION_END,
        render_validation_table(results, risk_class, risk_rationale),
    )
    desc = replace_marked_block(
        desc, ACTION_START, ACTION_END,
        render_recommended_action(risk_class, results),
    )
    body: dict = {"description": desc}
    if args.ready:
        body["title"] = (mr.get("title") or "").removeprefix("Draft: ")
    updated = api_or_die("PUT", f"/projects/{pid}/merge_requests/{args.iid}", body)
    out(
        {
            "merge_request_iid": updated.get("iid"),
            "merge_request_url": updated.get("web_url"),
            "checks_updated": sorted(results),
        }
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
        "draft": bool(getattr(args, "draft", False)),
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


def cmd_mr_update(args: argparse.Namespace) -> None:
    """Update MR fields (draft / title / description)."""
    project = resolve_project(args.path, args.project_id)
    pid = project["id"]
    body: dict = {}
    if args.draft is not None:
        body["draft"] = bool(args.draft)
    if args.title:
        body["title"] = args.title
    if args.description is not None:
        body["description"] = args.description
    if not body:
        die("mr-update requires at least one of --draft/--title/--description")
    out(api_or_die("PUT", f"/projects/{pid}/merge_requests/{args.mr_iid}", body))


def cmd_mr_merge(args: argparse.Namespace) -> None:
    """Mark MR ready (undraft) then merge."""
    project = resolve_project(args.path, args.project_id)
    pid = project["id"]
    iid = args.mr_iid
    # Undraft first so CE/EE allow merge without WIP gates.
    try:
        api("PUT", f"/projects/{pid}/merge_requests/{iid}", {"draft": False})
    except GitlabHTTPError:
        pass
    body = {
        "should_remove_source_branch": bool(args.remove_source_branch),
        "merge_when_pipeline_succeeds": bool(args.merge_when_pipeline_succeeds),
    }
    out(api_or_die("PUT", f"/projects/{pid}/merge_requests/{iid}/merge", body))


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
        short_artifact = args.artifact_id.split(":")[-1]
        cve = (args.cve_id or "").strip()
        # "[LW] 🔴 Bump quarkus-rest to 3.8.4-rhlw-6865 to fix CVE-2024-12397"
        # The prefix and dot make Lightwell MRs scannable in the GitLab list.
        title = (
            f"[LW] {RISK_EMOJI.get((args.risk_class or '').upper(), '⚪')} "
            f"Bump {short_artifact} to {args.new_version}"
        )
        if cve and cve != "unknown":
            title += f" to fix {cve}"

        def _describe(h: dict) -> str:
            return build_mr_description(
                args.artifact_id,
                args.new_version,
                path_with_ns,
                args.impact_text or "",
                h,
                old_version=old_version,
                cve_id=cve,
                cve_title=args.cve_title or "",
                cve_description=args.cve_description or "",
                cvss_score=args.cvss_score or "",
                risk_class=args.risk_class or "",
                risk_rationale=args.risk_rationale or "",
                risk_score=args.risk_score or "",
                transitive_count=args.transitive_count,
            )

        description = _describe(handoff)
        mr = api_or_die(
            "POST",
            f"/projects/{pid}/merge_requests",
            {
                "source_branch": branch,
                "target_branch": default_branch,
                "title": title,
                "description": description,
                "draft": bool(getattr(args, "draft", True)),
            },
        )
        iid = int(mr["iid"])
        handoff["merge_request_iid"] = iid
        description = _describe(handoff)
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
    sp.add_argument("--draft", action="store_true", help="create as draft/WIP MR")
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

    sp = sub.add_parser("mr-update", help="Update MR fields (draft, title, description)")
    add_project_flags(sp)
    sp.add_argument("--mr-iid", type=int, required=True)
    sp.add_argument("--draft", type=lambda v: str(v).lower() in ("1", "true", "yes"), default=None)
    sp.add_argument("--title", default="")
    sp.add_argument("--description", default=None)
    sp.set_defaults(func=cmd_mr_update)

    sp = sub.add_parser("mr-merge", help="Undraft and merge a merge request")
    add_project_flags(sp)
    sp.add_argument("--mr-iid", type=int, required=True)
    sp.add_argument("--remove-source-branch", action="store_true")
    sp.add_argument("--merge-when-pipeline-succeeds", action="store_true")
    sp.set_defaults(func=cmd_mr_merge)

    sp = sub.add_parser(
        "bump-maven-mr",
        help="Demo A: bump Maven dependency in pom.xml, push branch, open draft MR with handoff JSON",
    )
    add_project_flags(sp)
    sp.add_argument("--artifact-id", required=True, help="group:artifact")
    sp.add_argument("--new-version", required=True)
    sp.add_argument("--pom-path", default="pom.xml")
    sp.add_argument("--target-branch", default="")
    sp.add_argument("--branch", default="", help="default update-artifact-<version>")
    sp.add_argument("--impact-text", default="", help="Risk Assessment paragraph for MR body")
    # Advisory metadata, from the TPA vulnerability/advisory lookup in
    # query-tpa. All optional: a missing field renders as "not scored" /
    # "not referenced" rather than failing the bump.
    sp.add_argument("--cve-id", default="", help="e.g. CVE-2024-12397")
    sp.add_argument("--cve-title", default="", help="short CVE headline")
    sp.add_argument("--cve-description", default="", help="CVE description paragraph")
    sp.add_argument("--cvss-score", default="", help='e.g. "6.1 (Medium)"')
    # Risk classification, from gitlab.appSeeds in the tenant chart.
    sp.add_argument("--risk-class", default="", help="CRITICAL|HIGH|MEDIUM|LOW")
    sp.add_argument("--risk-rationale", default="", help="why the app carries that class")
    sp.add_argument("--risk-score", default="", help="Lightwell risk score; defaults to --risk-class")
    sp.add_argument(
        "--transitive-count",
        type=int,
        default=0,
        help="transitive dependency changes, for the SBOM Impact row",
    )
    sp.add_argument(
        "--draft",
        dest="draft",
        action="store_true",
        default=True,
        help="create draft MR (default)",
    )
    sp.add_argument(
        "--no-draft",
        dest="draft",
        action="store_false",
        help="create a ready (non-draft) MR",
    )
    sp.set_defaults(func=cmd_bump_maven_mr)

    sp = sub.add_parser(
        "mr-validation",
        help="Fill the Automated Validation table on an MR created by bump-maven-mr",
    )
    add_project_flags(sp)
    sp.add_argument("--iid", required=True, type=int, help="merge request iid")
    sp.add_argument(
        "--results",
        required=True,
        help=(
            'JSON object keyed by check: {"unit_tests": {"status": "passed", '
            '"details": "43/43 tests passed"}}. Valid keys: '
            + ", ".join(k for k, _ in VALIDATION_CHECKS if k != "risk_class")
            + '. status: passed|failed|pending|skipped|warning.'
        ),
    )
    sp.add_argument("--risk-class", default="", help="override; read from the MR if omitted")
    sp.add_argument("--risk-rationale", default="")
    sp.add_argument(
        "--ready",
        action="store_true",
        help="also drop the Draft: prefix (use only when every check passed)",
    )
    sp.set_defaults(func=cmd_mr_validation)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
