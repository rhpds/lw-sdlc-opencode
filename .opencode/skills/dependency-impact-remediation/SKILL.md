---
name: dependency-impact-remediation
description: Consume blast-radius handoff, bump a Maven dependency via gitlab_api.py, and open a merge request with agent-handoff JSON.
---

# Dependency impact remediation

Automates **Agent 1** behavior (`spec.md` §4). All GitLab mutations go through **`python3 /app/scripts/gitlab_api.py`** — see `.opencode/reference/gitlab-mcp.md`.

Demo depth **A**: remediate **one** application from EDA blast radius (`affected_repos[0]`), typically `lw-demo-help-app` / `help-im-vulnerable`.

## Inputs

Session JSON (from EDA `trigger-impact-analyzer`):

```json
{
  "artifact_id": "org.json:json",
  "new_version": "20220320.0.0.rhlw-00003",
  "gitlab_path": "lightwell/lw-demo-help-app-GUID",
  "repo_url": "https://gitlab.../lightwell/lw-demo-help-app-GUID.git",
  "affected_repos": [
    {
      "gitlab_path": "lightwell/lw-demo-help-app-GUID",
      "repo_url": "https://gitlab.../lightwell/lw-demo-help-app-GUID.git",
      "match_reason": "sbom_label_and_dependency_coordinate",
      "sbom_label": "sdlc-demo-GUID"
    }
  ],
  "blast_radius": { "mode": "single_app_demo_a", "count": 1 }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `artifact_id` | yes | Maven coordinate `group:artifact` |
| `new_version` | yes | Target version to adopt |
| `gitlab_path` or `affected_repos[0].gitlab_path` | yes (demo A) | GitLab project path |
| `repo_url` | recommended | HTTPS clone URL |

Environment: `GITLAB_URL`, `GITLAB_PAT` per `.opencode/reference/gitlab-credentials.md`.

## Preconditions

1. `GITLAB_PAT` is set (`echo` must not print empty). Do not invent tokens.
2. Do not load skill `mr-verify-ephemeral`.
3. If `blast_radius.count` is `0` or `affected_repos` is empty: **stop**—do not open an MR.

## Step 1 — Resolve target repo

1. Prefer **`gitlab_path`** / **`repo_url`**, else `affected_repos[0]`.
2. Optional check:
   ```bash
   python3 /app/scripts/gitlab_api.py project-get --path "<gitlab_path>"
   ```
3. Process **only that one** project.

## Step 2 — Draft risk assessment

Write a short **Risk Assessment** paragraph: why this application is or is not
affected, and whether human review is required and why. Keep it factual; you
pass it as `--impact-text`.

## Step 3 — Bump + MR (preferred one-shot)

Run:

```bash
python3 /app/scripts/gitlab_api.py bump-maven-mr \
  --path "<gitlab_path>" \
  --artifact-id "<artifact_id>" \
  --new-version "<new_version>" \
  --impact-text "<your Risk Assessment paragraph>" \
  --cve-id "<package_info.cve_id>" \
  --cve-title "<package_info.cve_title>" \
  --cve-description "<package_info.cve_description>" \
  --cvss-score "<package_info.cvss_score>" \
  --risk-class "<risk_class>" \
  --risk-rationale "<risk_rationale>" \
  --transitive-count 0
```

Pass every advisory field the session input gives you — they populate the MR
Summary table. Omit a flag only when the input genuinely lacks it; the CLI then
renders "not scored" / "not referenced". **Do not invent a CVE, a CVSS score or
a risk class.** An MR that overstates what is known is worse than one that
admits a gap.

`--transitive-count` is the number of *transitive* dependency changes the bump
causes — 0 for a straight version bump in `<dependencies>`. Only raise it if you
actually inspected the resolved tree.

The CLI writes the **Automated Validation** table with every row Pending. Do not
try to fill it in: the MR Verifier owns those results and rewrites that block
once the verify Job has really run.

Default: open a **draft** MR (`--draft`; use `--no-draft` only if explicitly required). AO merges after Verifier (+ Approval when policy says `needs_approval`).

The CLI:

1. Reads `pom.xml` from the default branch
2. Creates branch `update-artifact-<sanitized-version>`
3. Commits the dependency version bump and, for the demo app, updates the matching `/api/status` `lightwellFix` marker in `HelloController.java`
4. Opens a **draft** MR whose description includes the human template **and** the `<!-- agent-handoff: do not edit below -->` JSON block

Stdout JSON includes `merge_request_iid`, `merge_request_url`, `project_id`, `branch`. When input JSON has `"draft_mr": true` (AO path), always keep the MR as draft.

### Manual fallback (only if one-shot fails)

Use `branch-create`, `file-get`, edit locally, `commit-file`, `mr-create` as documented in `.opencode/reference/gitlab-mcp.md`. MR description MUST still include the handoff block:

```markdown
## Dependency Update
Artifact: `<artifact_id>`
New Version: `<new_version>`
GitLab path: `<gitlab_path>`
Blast radius: demo A (single app)

## Impact Analysis
<analysis>

<!-- agent-handoff: do not edit below -->
```json
{
  "artifact_id": "<artifact_id>",
  "new_version": "<new_version>",
  "source_branch": "update-artifact-<sanitized-version>",
  "target_branch": "main",
  "project_id": 12345,
  "merge_request_iid": 67,
  "gitlab_path": "<gitlab_path>"
}
```
```

## Failure handling

| Condition | Action |
|-----------|--------|
| Empty blast radius | Stop; no MR |
| Project not found | Stop; report path |
| CLI HTTP error | Stop; include stderr; retry at most 2 times |
| Dependency missing in pom | Stop; report artifact_id |

## Completion

Return summary: GitLab project, MR URL, `merge_request_iid`, and confirmation that agent-handoff JSON is present.
