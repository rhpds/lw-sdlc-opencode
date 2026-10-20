# GitLab API CLI (OpenCode)

Credentials: [gitlab-credentials.md](./gitlab-credentials.md) (`GITLAB_URL`, `GITLAB_PAT`).

Demo A does **not** use GitLab MCP. Agents call the baked-in CLI:

```text
python3 /app/scripts/gitlab_api.py <subcommand> ...
```

Token auth is `PRIVATE-TOKEN` from `GITLAB_PAT` (or `GITLAB_TOKEN`). Official GitLab MCP (`/api/v4/mcp`) requires GitLab ≥18.6 and OAuth — not available on workshop GitLab 17.x.

## Commands

| Command | Purpose |
|---------|---------|
| `project-get --path GROUP/PROJECT` | Resolve project JSON (includes `id`) |
| `file-get --path … --file-path pom.xml --ref main` | Raw file contents |
| `branch-create --path … --branch NAME --ref main` | Create branch (idempotent) |
| `commit-file --path … --branch … --file-path … --message … --content-file …` | Commit update |
| `mr-create --path … --source-branch … --title … --description …` | Open MR |
| `mr-get --project-id N --mr-iid N` | Fetch MR (description / handoff) |
| `mr-note --project-id N --mr-iid N --body "…"` | Post MR note |
| `mr-validation --project-id N --iid N --results '{…}'` | Rewrite the Automated Validation table between the `lw:validation` markers. Note `--iid`, **not** `--mr-iid`. Any check key you omit is rendered Pending |
| `bump-maven-mr --path … --artifact-id g:a --new-version V --impact-text "…"` | **Demo A one-shot**: bump `pom.xml`, branch, MR + handoff JSON |

All successful commands print JSON on stdout (except `file-get` without `--json`).

## Demo A preferred path

```bash
python3 /app/scripts/gitlab_api.py bump-maven-mr \
  --path lightwell/lw-demo-help-app-<guid> \
  --artifact-id org.json:json \
  --new-version 20220320.0.0.rhlw-00003 \
  --impact-text "Remediating CVE for demo A single-app blast radius."
```

## Verifier

```bash
python3 /app/scripts/gitlab_api.py mr-get --project-id <id> --mr-iid <iid>
python3 /app/scripts/gitlab_api.py mr-note --project-id <id> --mr-iid <iid> --body "Verify summary…"
```

## Skills

- `dependency-impact-remediation` — prefer `bump-maven-mr`
- `mr-verify-ephemeral` — `mr-get` / `mr-note` only for GitLab I/O

Do **not** call GitLab REST with ad-hoc `curl` from the agent; use this CLI so auth and errors stay consistent.
