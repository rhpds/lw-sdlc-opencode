Technical specification for the Autonomous Software Supply Chain Remediation Loop. Integration boundaries, API schemas, agent skills, MCP configuration, and OpenShift manifests are defined so an autonomous agent or engineer can implement the system without a custom Java control plane.

**Supersedes:** [docs/deprecated/spec-quarkus-langgraph.md](docs/deprecated/spec-quarkus-langgraph.md) (Quarkus, LangGraph4j, PostgreSQL harness — deprecated).

---

# Technical Specification: Autonomous Software Supply Chain Remediation System

## 1. System Overview

This system automates ingestion, security analysis, codebase remediation, and ephemeral testing when new software dependencies are published. The architecture spans Nexus, Event-Driven Ansible (EDA), Red Hat Trusted Profile Analyzer (RHTPA), GitLab, and OpenShift.

**Control plane:** [OpenCode](https://opencode.ai/docs/server/) running headless (`opencode serve`) in a container on OpenShift. Orchestration logic lives in **Agent Skills** (`SKILL.md`) and **named agents** in `opencode.json`, not in Quarkus or LangGraph4j.

**Event automation (Ansible EDA):** Webhook ingress, TPA API calls, and OpenCode HTTP triggers run as **EDA rulebooks + playbooks** in this repository (`eda-rulebooks/`, `playbooks/`). Ansible is the **control plane for the remediation event chain**, not for provisioning cluster infrastructure.

**Runtime / platform (Argo CD GitOps):** Workloads **owned by this project** (OpenCode, integration secrets, optional MCP) are installed and updated via **Git** under `gitops/` and `openshift/` (Argo CD Applications reconcile). **Shared platform** components (Keycloak, AAP/EDA, RHTPA, RHTAS, GitLab, JFrog Artifactory, Jenkins, SonarQube, Quay, Argo CD itself) may be **pre-installed** in the managed environment—see [`docs/reference/infra-platform-stack.md`](docs/reference/infra-platform-stack.md). This demo does **not** use Artifactory or Jenkins for the remediation loop. Do **not** use Ansible playbooks like a Lightwell `demo-setup.sh deploy` to install that platform layer; EDA **runs** rulebooks from git on the existing AAP/EDA stack.

**Artifact note:** **Sonatype Nexus** (repository webhooks) is the artifact ingress for EDA—not **JFrog Artifactory**. Artifactory OSS does not support the webhooks this loop needs (Enterprise-only); see [`docs/reference/infra-platform-stack.md`](docs/reference/infra-platform-stack.md). `gitops/nexus/` configures Lightwell repos and Nexus → EDA webhooks (§3.1).

**Non-deterministic work** (impact analysis, code changes, MR text, verify orchestration) runs in **OpenCode** with MCP tools. Ephemeral verify namespaces and Jobs created by `mr-verifier` remain agent-driven exceptions per §5 (not GitOps steady state).

### 1.1 Trigger and flow (demo depth A)

1. **Nexus (webhook)** → **Ansible EDA** (listens on `:5000`)
2. **Ansible EDA** → **`query-tpa.yml`** → **RHTPA** (blast radius by SBOM label) → POST **`tpa_results`** (`affected_repos`, `blast_radius`)
3. If `blast_radius.count >= 1`: **EDA** → **OpenCode** (`impact-analyzer`, skill `dependency-impact-remediation`) for **`affected_repos[0]`** (help-app)
4. **Agent `impact-analyzer`** → **GitLab MCP** (branch, commit, open merge request)
5. **GitLab webhook** → **Ansible EDA** → **OpenCode** (`mr-verifier`, skill `mr-verify-ephemeral`)
6. **Agent `mr-verifier`** → **OpenShift Job** (`mvn clean verify`) + ephemeral namespace/Route
7. **Agent `mr-verifier`** → **GitLab MCP** (MR note)

**Not in demo A:** promote/merge to production; multi-app fan-out (same `tpa_results` shape reserved for later).

```
┌────────┐    ┌─────┐    ┌─────┐    ┌──────────────────┐    ┌──────────┐
│ Nexus  │───▶│ EDA │───▶│ TPA │    │ OpenCode (OCP)   │───▶│ GitLab   │
└────────┘    │     │    └─────┘    │ impact-analyzer  │    │ MR       │
              │     │───────────────▶│ mr-verifier      │◀───│ webhook  │
              └─────┘                └────────┬─────────┘    └──────────┘
                                              │
                                              ▼
                                    Job + ephemeral NS (not prod)
```

**GitOps:** `lightwell-workshop` `bootstrap-infra` / `bootstrap-tenant`. **SCM:** this repository.
### 1.2 Design principles

| Principle | Requirement |
|-----------|-------------|
| No custom JVM harness | Do not implement Agent 1/2 as Quarkus services. |
| Skills as workflow | Multi-step behavior is documented in `.opencode/skills/*/SKILL.md` and invoked via the `skill` tool. |
| MCP for GitLab | Use GitLab MCP (HTTP or stdio); avoid bespoke GitLab REST clients in application code. |
| Isolated builds | Unit tests and compiles run in disposable OpenShift workloads, not in the long-lived OpenCode Deployment. |
| Stable handoff | MR descriptions include a machine-readable JSON block (§4.4) for the verifier agent and optional Ansible gates. |
| GitOps platform | Argo CD reconciles cluster infrastructure from Git; OpenCode image tag/digest is promoted via GitOps (§7). |
| EDA ingress | **Chosen path:** Nexus and GitLab webhooks → Ansible EDA → playbooks → OpenCode. GitLab CI trigger is documented only as a future option (§3.5). |
| Ansible scope | Ansible **only** for EDA rulebooks/playbooks (events). **No** Ansible-driven cluster/infra deploy. |
| GitOps scope | Argo CD owns **all** infra manifests; image promotion via Git after CI (§7–8). |

### 1.3 Repository layout (target)

```
.
├── spec.md
├── opencode.json
├── .opencode/
│   ├── agents/
│   │   ├── impact-analyzer.md
│   │   └── mr-verifier.md
│   ├── reference/
│   │   ├── gitlab-credentials.md
│   │   └── gitlab-mcp.md
│   └── skills/
│       ├── dependency-impact-remediation/
│       │   └── SKILL.md
│       └── mr-verify-ephemeral/
│           └── SKILL.md
├── container/
│   └── Dockerfile
├── .github/
│   └── workflows/
│       └── ci-opencode-image.yml
├── gitops/                    # Argo CD Applications, integration config, workloads
│   ├── README.md              # Start: fill base/cluster-config, then secrets, then sync apps
│   ├── base/cluster-config/   # ConfigMap sdlc-cluster-config (non-secret URLs)
│   ├── config/                # integration-catalog + early config-only Argo path
│   ├── secrets/               # ExternalSecret examples + contract
│   ├── argocd/applications/
│   ├── sdlc-control-plane/
│   ├── sdlc-eda/
│   └── nexus/
│       └── README.md
├── docs/reference/
│   └── nexus-webhook-ansible-baseline.md   # API steps ported from Ansible → GitOps
├── openshift/                 # Manifests synced by Argo CD (or referenced Kustomize paths)
│   ├── deployment.yaml
│   ├── route.yaml
│   ├── serviceaccount-verifier.yaml
│   └── templates/
│       ├── verify-job.yaml
│       └── ephemeral-namespace.yaml
├── eda-rulebooks/             # Synced/mounted by EDA (GitOps deploys EDA; content lives in git)
│   └── sdlc-remediation.yml
├── playbooks/                 # Invoked by EDA only — not for infra install
│   ├── query-tpa.yml
│   ├── trigger-impact-analyzer.yml
│   └── trigger-mr-verifier.yml
├── gitlab/
│   ├── ci/opencode-mr-verifier.yml
│   ├── scripts/trigger-opencode-agent.sh
│   └── .gitlab-ci.yml.example
```

### 1.4 Ansible vs Argo CD (responsibility split)

| Concern | Tool | Examples |
|---------|------|----------|
| **Infrastructure (this repo)** | **Argo CD GitOps** | OpenCode Deployment, Routes, MCP server, RBAC, ExternalSecrets, optional integration Jobs |
| **Platform (pre-installed)** | **Ops / platform team** | Keycloak, AAP+EDA, RHTPA, RHTAS, GitLab, Quay, Argo CD, etc. — see [`infra-platform-stack.md`](docs/reference/infra-platform-stack.md); Artifactory/Jenkins not used here |
| **Event wiring** | **EDA + platform config** | **Nexus** + GitLab webhooks → existing EDA; rulebook content from this git repo |
| **Event chain** | **Ansible EDA** | Rulebooks on `:5000`, `query-tpa.yml`, `trigger-impact-analyzer.yml`, `trigger-mr-verifier.yml`, `tpa_results` callbacks |
| **Application image** | **GitHub Actions → Quay** | Container build; GitOps updates image tag/digest in Git |
| **Ephemeral test resources** | **OpenCode agent (`oc`)** | `pr-test-mr-<guid>-*` namespaces, verify Jobs (exception to GitOps steady state) |

EDA is **deployed** by GitOps; EDA **runs** Ansible logic from git when webhooks arrive. Reference demos that use `ansible-playbook deploy.yml` for the whole stack (e.g. Lightwell `demo-setup.sh`) are **not** the pattern for this project’s infrastructure—only borrow their **TPA query / `tpa_results`** playbook ideas for EDA actions.

---

## 2. Infrastructure prerequisites

* **Red Hat TPA:** On OpenShift; OIDC for automation (`TPA__OIDC__WALKER_CLIENT_SECRET` or equivalent token flow in Ansible).
* **GitLab:** Self-hosted; authenticate with **`GITLAB_USERNAME`** and **`GITLAB_PASSWORD`** (service account). A GitLab PAT is **not** provisioned manually—when GitLab MCP or the API requires a token, it is **derived at runtime** from those credentials (§6.0).
* **GitLab MCP:** Cluster service in namespace `sdlc-mcp-servers`, or stdio GitLab MCP in the OpenCode image.
* **OpenShift:** Optional `RuntimeClass` `kata` (or gVisor) for verify Jobs; namespace `sdlc-sandboxes-<guid>` for isolated builds.
* **Argo CD:** GitOps controller on the cluster; Applications watch this repo (or a deployment repo) for `gitops/` and `openshift/` paths. Cluster bootstrap may require a one-time Argo install; thereafter **all infrastructure** changes flow through Git merge + sync—not Ansible deploy playbooks.
* **Event-Driven Ansible (EDA):** Deployed and configured via **GitOps** (operator, activation, rulebook mount from git). At runtime, EDA executes **only** event playbooks (`playbooks/`)—never used to install OpenCode, MCP, or namespaces.
* **Sonatype Nexus:** Repository webhooks pointing at EDA are **GitOps-managed** (§3.1); baseline API behavior is documented from the prior Ansible implementation in [`docs/reference/nexus-webhook-ansible-baseline.md`](docs/reference/nexus-webhook-ansible-baseline.md).
* **OpenCode:** Container image built from `container/Dockerfile`, published to Quay (§8); Route or internal Service `opencode.sdlc-control-plane.svc.cluster.local`.
* **LLM provider:** API credentials mounted as Secrets; model IDs configured per agent in `opencode.json`.
* **GitHub Actions:** Workflow pushes the image to `quay.io` on merges to `main` and on version tags.

---

## 3. Ingestion pipeline

### 3.1 Sonatype Nexus webhook (GitOps-managed)

Nexus must POST component events to EDA when artifacts are published. **Webhook capabilities are configured via Argo CD GitOps** (`gitops/nexus/`), not via Ansible deploy playbooks. Ansible in this project is only for **EDA event playbooks** after the webhook fires (§1.4).

**EDA target URL (in-cluster):** `http://<eda-webhook-service>.<namespace>.svc.cluster.local:5000/`  
(Path segment may match rulebook mount, e.g. `/` or `/nexus-upload`—rulebook and Nexus URL must agree.)

**Payload:** Nexus component webhook (`component.name`, `component.version`, `component.format`, `action` e.g. `CREATED`). Custom demos may instead emit `vulnerability_fix_published` at the edge; EDA rules in §3.2 / `sdlc-remediation.yml` should accept the chosen shape.

#### 3.1.1 GitOps configuration contract

| Item | Requirement |
|------|-------------|
| **Delivery** | Argo CD Application syncs `gitops/nexus/` (Kustomize: Lightwell Maven repos + EDA webhooks via PostSync Job) |
| **Idempotency** | Before create, list `GET /service/rest/v1/capabilities`; skip if same `repository` + `url` exists |
| **Nexus API** | Prefer **ExtDirect** `POST /service/extdirect` with `typeId: webhook.repository`, `names: component`, `properties.url` = EDA URL (REST create may 500 on some Nexus OSS builds) |
| **Repositories** | One webhook capability per hosted Maven repo (parameterize list; Lightwell used `redhat-packages-validated` / `redhat-packages-remediated`) |
| **Secrets** | `NEXUS_ADMIN_PASSWORD` from ExternalSecrets/SealedSecrets; never in git |
| **Reference** | API steps ported from Ansible: [`docs/reference/nexus-webhook-ansible-baseline.md`](docs/reference/nexus-webhook-ansible-baseline.md) |

#### 3.1.2 Ordering

1. GitOps: EDA Service listening on `:5000` (EDA Application healthy).
2. GitOps: Nexus webhook reconcile Job runs (depends on EDA URL).
3. Runtime: publish component → Nexus webhook → EDA rule → `query-tpa.yml` / OpenCode chain.

### 3.2 EDA rulebook (unified)

**File:** `eda-rulebooks/sdlc-remediation.yml`

Single webhook source on port **5000** with three runtime rules (plus a catch-all debug rule):

| Rule | Condition | Playbook |
|------|-----------|----------|
| Nexus / vulnerability | `action == CREATED` or `type == vulnerability_fix_published` | `playbooks/query-tpa.yml` |
| TPA callback | `type == tpa_results` with `affected_repos` | `playbooks/trigger-impact-analyzer.yml` |
| GitLab MR | `merge_request` + `opened` + `source_branch` ~ `update-artifact-*` | `playbooks/trigger-mr-verifier.yml` |

Activation **extra_vars** are documented in `eda/README.md` and `eda/example-extra-vars.yml`. On AAP, replace `run_playbook` with `run_job_template` for the same playbooks.

### 3.3 Two-phase flow: TPA query → OpenCode (Agent 1)

**Phase 1 — `playbooks/query-tpa.yml`**

1. Keycloak password grant → TPA bearer token (`playbooks/tasks/get-tpa-token.yml`).
2. `GET {{ tpa_url }}/api/v2/sbom?labels.name={{ tpa_sbom_label }}`.
3. Build `affected_repos` and `package_info` (Lightwell `tpa_results` contract).
4. `POST {{ eda_webhook_url }}` with body `{ "type": "tpa_results", "affected_repos", "package_info" }`.

Optional SBOM upload before query: `playbooks/upload-sbom-tpa.yml` (not on the default Nexus path).

**Phase 2 — `playbooks/trigger-impact-analyzer.yml`**

Maps `package_info` / `affected_repos` to skill input and calls OpenCode via `playbooks/tasks/opencode-prompt-async.yml` (agent `impact-analyzer`, skill `dependency-impact-remediation`).

**Compatibility alias (optional):** A thin HTTP adapter MAY expose `POST /api/v1/agents/impact-analyzer` with the legacy body from the deprecated spec and translate it to the OpenCode calls above. New implementations SHOULD call OpenCode directly.

**Legacy request body (for adapters):**

```json
{
  "artifact_id": "org.apache.logging.log4j:log4j-core",
  "new_version": "2.17.1"
}
```

### 3.4 GitLab MR via same rulebook — **chosen path**

GitLab instance or project webhooks POST to the **same EDA listener** as Nexus (port 5000). Filtering is in `sdlc-remediation.yml` (not a separate `gitlab-mr-trigger.yml`).

* **Condition:** `object_kind == "merge_request"` AND `object_attributes.state == "opened"` AND `object_attributes.source_branch` matches `update-artifact-*`.
* **Playbook:** `playbooks/trigger-mr-verifier.yml` — agent `mr-verifier`, skill `mr-verify-ephemeral`, fields `merge_request_iid`, `project_id`, `source_branch`, `target_branch`, `repository_git_url`.

### 3.5 GitLab CI trigger — **future option (not in use)**

> **Note:** We are **not** using this path today. Merge requests are triggered via **§3.4 (EDA)** only. The files under `gitlab/` are kept so the same OpenCode API call (`/session`, `/prompt_async`, agent `mr-verifier`) can be invoked later from a **`.gitlab-ci.yml`** in an application repo—useful if a team prefers MR pipelines over instance webhooks, or runners already have a trust path to OpenCode without routing through EDA.

If adopted in the future, a job on `merge_request_event` would mirror `playbooks/trigger-mr-verifier.yml` using:

* **Templates:** `gitlab/ci/opencode-mr-verifier.yml`, `gitlab/scripts/trigger-opencode-agent.sh`, example `gitlab/.gitlab-ci.yml.example`
* **CI variables:** `OPENCODE_BASE_URL`, `OPENCODE_SERVER_PASSWORD` (masked)
* **Do not** run EDA webhooks and GitLab CI triggers for the same MR without coordination (duplicate verifier sessions).

See [`gitlab/README.md`](gitlab/README.md).

---

## 4. Agent: impact-analyzer

### 4.1 OpenCode agent definition

**File:** `.opencode/agents/impact-analyzer.md` (or `agents` block in `opencode.json`)

* **mode:** `primary`
* **Permissions:** allow `read`, `edit`, `grep`, `glob`, `skill`, GitLab MCP tools; deny `shell` except allowlisted package-manager invocations if required.
* **system:** Short pointer to load `dependency-impact-remediation` at session start.

### 4.2 Skill: dependency-impact-remediation

**File:** `.opencode/skills/dependency-impact-remediation/SKILL.md`

Workflow steps (replaces LangGraph nodes):

1. **Locate impact** — Query TPA / SBOM graph (HTTP from skill instructions) for internal repos that depend on `artifact_id`.
2. **Branch and modify** — GitLab MCP: `create_branch` with pattern `update-artifact-<version-sanitized>`. Bump dependency via documented command (`mvn versions:use-dep-version`, `npm install`, etc.).
3. **Analyze code** — Read release notes; search repo for deprecated APIs; summarize in MR body.
4. **Create MR** — GitLab MCP `create_merge_request`; title `chore(deps): update <artifact_id> to <new_version>`.

The skill MUST require loading GitLab MCP for all GitLab mutations.

### 4.3 Human-readable MR template

```markdown
## Dependency Update
Artifact: `<artifact_id>`
New Version: `<new_version>`

## Impact Analysis
<free text>
```

### 4.4 Machine handoff block (required)

The MR description MUST end with an HTML comment marker `<!-- agent-handoff: do not edit below -->` followed by a fenced JSON block for `mr-verifier` and optional Ansible validation:

```json
{
  "artifact_id": "org.apache.logging.log4j:log4j-core",
  "new_version": "2.17.1",
  "source_branch": "update-artifact-2.17.1",
  "target_branch": "main",
  "project_id": 12345,
  "merge_request_iid": 67
}
```

Ansible MAY reject the webhook if this block is missing when `strict_handoff: true`.

---

## 5. Agent: mr-verifier

### 5.1 OpenCode agent definition

**File:** `.opencode/agents/mr-verifier.md`

* **mode:** `primary`
* **Permissions:** allow `skill`, `shell` limited to `oc`/`kubectl` with verifier ServiceAccount; deny in-repo `edit` on control-plane workspace.
* **ServiceAccount:** `mr-verifier` with RBAC to create namespaces matching `pr-test-mr-<guid>-*`, Jobs in `sdlc-sandboxes-<guid>`, and Routes in those namespaces.

### 5.2 Skill: mr-verify-ephemeral

**File:** `.opencode/skills/mr-verify-ephemeral/SKILL.md`

Workflow steps:

1. **Parse handoff** — Extract `agent-handoff` JSON from MR description (GitLab MCP).
2. **Compile and unit test** — Apply `openshift/templates/verify-job.yaml` with `runtimeClassName: kata` when available; image includes JDK + Maven; clone `source_branch`; run `mvn clean verify`. Poll Job logs; fail closed on non-zero exit.
3. **Deploy ephemeral app** — Create namespace `pr-test-mr-<guid>-<merge_request_iid>`; apply Deployment and Route from `openshift/templates/ephemeral-namespace.yaml` (or equivalent Helm/Kustomize path documented in the skill).
4. **Smoke tests** — Run Playwright or Postman collection against the Route URL (Job or short-lived Pod in the ephemeral namespace).
5. **Summarize** — GitLab MCP `create_merge_request_note` with build status, test summary, Route URL, and namespace TTL.

**Forbidden:** Running `mvn clean verify` on the OpenCode Deployment pod filesystem.

### 5.3 Verify Job template

**File:** `openshift/templates/verify-job.yaml`

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  generateName: verify-mr-
  namespace: sdlc-sandboxes-<guid>
spec:
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      runtimeClassName: kata
      restartPolicy: Never
      containers:
        - name: verify
          image: registry.example/sdlc/maven-verify:latest
          env:
            - name: GIT_URL
              value: "REPLACE_REPOSITORY_GIT_URL"
            - name: GIT_BRANCH
              value: "REPLACE_SOURCE_BRANCH"
            - name: BUILD_COMMAND
              value: "mvn clean verify"
```

The skill instructs the agent to substitute env values and `oc apply -f`.

### 5.4 Ephemeral deploy (illustrative)

Namespace: `pr-test-mr-<guid>-<merge_request_iid>`. Deployment name: `ephemeral-app`. Image: project pipeline output or prebuilt smoke image documented per demo app. Route: `ephemeral-app-pr-test-mr-<guid>-<iid>.apps.<cluster>`.

Implement via `oc apply` and templates in-repo; do not embed Fabric8 or Java clients.

---

## 6. MCP and OpenCode configuration

### 6.0 GitLab authentication (username / password)

Operators provide **username and password** only. Do not require a pre-created PAT in Git or in operator runbooks.

| Variable | Role |
|----------|------|
| `GITLAB_URL` | GitLab instance base URL |
| `GITLAB_USERNAME` | Bot / service user |
| `GITLAB_PASSWORD` | Password (from OpenShift Secret; GitOps references only) |
| `GITLAB_PAT` | **Derived** PAT or API token for `PRIVATE-TOKEN` headers—populated by init Job, GitLab MCP sidecar, or secret sync before OpenCode starts |

**Flow:**

1. Argo CD deploys OpenCode and GitLab MCP with Secrets containing username/password.
2. A bootstrap step (initContainer, Job, GitLab MCP own auth, or External Secrets template) uses those credentials to obtain or refresh a PAT with scopes `api`, `read_repository`, `write_repository` when the chosen integration requires it.
3. OpenCode MCP client reads `${GITLAB_PAT}` from the environment at runtime.

Details and integration patterns: [`.opencode/reference/gitlab-credentials.md`](.opencode/reference/gitlab-credentials.md).

### 6.1 `opencode.json` (project root)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "default_agent": "impact-analyzer",
  "agent": {
    "impact-analyzer": {
      "mode": "primary",
      "description": "TPA-aware dependency bump and MR creation",
      "permission": {
        "skill": { "*": "allow", "mr-verify-ephemeral": "deny" }
      }
    },
    "mr-verifier": {
      "mode": "primary",
      "description": "Isolated verify, ephemeral deploy, MR feedback",
      "permission": {
        "skill": { "dependency-impact-remediation": "deny", "mr-verify-ephemeral": "allow" },
        "bash": "ask"
      }
    }
  },
  "mcp": {
    "gitlab": {
      "type": "http",
      "url": "http://gitlab-mcp.sdlc-mcp-servers.svc.cluster.local/mcp",
      "headers": {
        "PRIVATE-TOKEN": "${GITLAB_PAT}"
      }
    }
  }
}
```

`${GITLAB_PAT}` is **runtime-derived** from `GITLAB_USERNAME` / `GITLAB_PASSWORD` (§6.0), not stored in git. If GitLab MCP is deployed with its own username/password auth, OpenCode may omit this header when connecting to that MCP Service.

For large GitLab tool surfaces, prefer [lazy-mcp](https://gitlab.com/gitlab-org/ai/lazy-mcp) as a single aggregated MCP endpoint.

Environment variables (`GITLAB_URL`, `GITLAB_USERNAME`, `GITLAB_PASSWORD`, derived `GITLAB_PAT`, LLM keys) come from OpenShift Secrets; reference them in GitOps manifests, not in git.

### 6.2 Server security

* Run `opencode serve --hostname 0.0.0.0 --port 4096` in the container entrypoint.
* Set `OPENCODE_SERVER_PASSWORD` (and optional `OPENCODE_SERVER_USERNAME`); EDA uses HTTP Basic auth.
* OpenAPI spec: `GET /doc` on the same base URL.

### 6.3 Session lifecycle

* **Async prompts:** `POST /session/{id}/prompt_async` returns `204`; monitor via `GET /event` (SSE) or session message APIs.
* **Idempotency:** EDA SHOULD pass `merge_request_iid` in the verifier prompt so repeated webhooks do not duplicate namespaces (skill: check existing `pr-test-mr-<guid>-*`).

---

## 7. OpenShift deployment (OpenCode) and GitOps

**Namespace:** `sdlc-control-plane`

Platform resources below are **managed by Argo CD**. Manifests live under `openshift/` and/or `gitops/argocd/`; merging to the tracked branch triggers sync. Do not rely on manual `oc apply` for production drift control. CI publishes the container image to Quay; **image promotion** (tag or digest in Git) is the GitOps contract between application CI and cluster runtime.

| Resource | Purpose |
|----------|---------|
| `Deployment` | OpenCode server + optional lazy-mcp sidecar |
| `Route` / `Service` | EDA and operators reach `:4096` |
| `Secret` | `OPENCODE_SERVER_PASSWORD`, LLM API key, `gitlab-credentials` (`username`, `password`; optional derived `token` / `GITLAB_PAT`) via ExternalSecrets / SealedSecrets—not committed |
| `ConfigMap` or image bake | `opencode.json`, `.opencode/skills`, `.opencode/agents` baked in image; optional ConfigMap overlay via GitOps |
| `ServiceAccount` + `RoleBinding` | `mr-verifier` agent credentials (mounted only when running verifier sessions via projected token or `oc` login) |
| `Application` (Argo CD) | Watches repo path for `sdlc-control-plane` stack; auto-sync or manual promote per policy |

Resource requests: sized for LLM orchestration, not for Maven builds.

**Image reference:** GitOps SHOULD pin `quay.io/sshaaf/sdlc-opencode` to an immutable `sha-<short-git-sha>` or semver tag from §8—not floating `latest` in production Argo CD parameters.

---

## 8. Continuous integration (GitHub Actions → Quay.io)

The OpenCode control-plane image is built in GitHub Actions and pushed to Quay.io. Ansible, EDA, and OpenShift consume the published tag; they do not build the image on-cluster.

### 8.1 Workflow

**File:** `.github/workflows/ci-opencode-image.yml`

| Job / event | Behavior |
|-------------|----------|
| `pull_request` to `main` | `build` → `test` (smoke); **no** `publish` |
| `push` to `main` | `build` → `test` → `publish` to Quay (`latest`, `sha-<short-sha>`) |
| `push` tag `v*` | Same pipeline; semver tags via `docker/metadata-action`; `release` job creates GitHub Release |
| `workflow_dispatch` | Full pipeline except PR skip rules |

Smoke test: `container/scripts/smoke-test.sh` (health API + baked config). See [README.md](README.md#container-image-ci).

Build context is the repository root; Dockerfile path is `container/Dockerfile`. The image bakes in `opencode.json` and `.opencode/` (agents and skills). Runtime secrets are **not** baked into the image.

### 8.2 Quay repository setup

1. Repository on [Quay.io](https://quay.io): **`sshaaf/sdlc-opencode`** (`quay.io/sshaaf/sdlc-opencode`).
2. Create a **robot account** or use a user with push access; grant **Write** on that repository.
3. Configure the GitHub repository:

| Name | Type | Value |
|------|------|--------|
| `QUAY_IMAGE_NAME` | Variable (optional) | Full name without tag; default in CI is `quay.io/sshaaf/sdlc-opencode` |
| `QUAY_USERNAME` | Secret | Robot or user name |
| `QUAY_PASSWORD` | Secret | Robot token or password |

If `QUAY_IMAGE_NAME` is unset, CI uses `quay.io/sshaaf/sdlc-opencode`.

### 8.3 Dockerfile contract

**File:** `container/Dockerfile`

* Base: `ghcr.io/anomalyco/opencode:latest`
* Adds `git` and `openssh-client` (Alpine `apk`) for agent workflows
* `WORKDIR /app` with `COPY opencode.json` and `COPY .opencode`
* `ENTRYPOINT`: `opencode serve --hostname 0.0.0.0 --port 4096`
* `EXPOSE 4096`

### 8.4 Supply-chain alignment

This pipeline is the **trusted build** for the same remediation loop Nexus triggers: dependency updates flow through TPA and GitLab, while the agent runtime image is versioned on Quay and referenced immutably in OpenShift when policy requires it (prefer `sha-<short-sha>` over floating `latest` in production).

---

## 9. Non-goals (this specification)

* Quarkus, LangGraph4j, PostgreSQL agent state store.
* Custom GitLab REST SDKs in Java or Python services.
* `SandboxClaim` CRD (`agents.k8s.io/v1alpha1`) unless the cluster already provides it; use `Job` / `PipelineRun` instead.
* In-pod compilation on the OpenCode control plane.

---

## 10. Migration note

Implementations started from the deprecated Quarkus specification should remap:

| Deprecated | Current |
|------------|---------|
| `POST .../api/v1/agents/impact-analyzer` | OpenCode session + `impact-analyzer` + skill §4.2 |
| `POST .../api/v1/agents/verify-mr` | EDA GitLab rulebook + OpenCode `mr-verifier` §5 |
| `application.properties` MCP | `opencode.json` §6 |
| Fabric8 deploy code | OpenShift templates + `oc` §5.3–5.4 |
| LangGraph nodes | Skill workflow steps §4.2, §5.2 |
