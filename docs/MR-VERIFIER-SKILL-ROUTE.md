# MR verifier: the skill route — ADR and handover

Branch: `experiment/mr-verifier-skill-route` (PR #9, **draft, do not merge**).
Written 2026-10-21 for Shaaf and whichever agent he points at this.

This is the handover note for testing. It records what the branch changes,
what has and has not been proven, and the three things most likely to trip
you up. The PR description carries the original experiment write-up; this
file is the decision record and the test plan.

---

## Why this branch exists

The validation table on a remediation merge request has eight rows. The
question is whether the **opencode agent** can fill them as trustworthily as
the Tekton pipeline does, because the agent route is where we want to end up.

The first run of this experiment found the agent filling rows it had not
measured — it copied plausible strings out of the skill's own worked example.
That is the failure this branch is built to make impossible.

## ADR — measure, never judge

| Field | Content |
|-------|---------|
| **Status** | Accepted for the branch; unproven end to end (see *What is not proven*) |
| **Context** | The skill described what a good answer looks like and gave the agent no way to obtain one, so under pressure to produce a finished-looking table it invented `license` and `regression` and omitted `signature` and `sbom_integrity`. Omission is not free either: **a key absent from `mr-validation` renders as `Pending` and erases whatever was in that row**, so a partial run silently wipes results another route measured. |
| **Decision** | Add `scripts/lw_checks.py`, a deterministic tool with one subcommand per check that prints `{"status", "details"}`. The agent runs it and pastes the result; it does not reason about the answer. All seven keys are now sent — none omitted. |
| **Consequences** | The fabrication path is gone: there is nothing to infer. The cost is that `regression` requires a second build (see below). The agent is still responsible for *running* the tools and for not rewriting a `warning` into a `passed`. |

### What each check actually does

| Key | How it is measured |
|-----|--------------------|
| `unit_tests` / `integration_tests` | Surefire / Failsafe summary lines in the verify Job log |
| `dependency_compat` | `BUILD SUCCESS`, or the resolution error that replaced it |
| `license` | Compares baseline and remediated POM licences, **walking the `<parent>` chain** |
| `regression` | Diffs the two builds the verify Job now runs |
| `signature` | Hashes the published JAR against the digest the artifact manager recorded |
| `sbom_integrity` | Asserts the CycloneDX SBOM lists the bumped coordinate |

Two of these are subtler than they look, and both were real bugs in the
Tekton route found while building this:

- **Licence must follow `<parent>`.** Most artifacts declare no `<licenses>`
  of their own. `commons-text` inherits Apache-2.0 from `commons-parent` →
  `org.apache:apache`. Reading only the artifact's own POM finds nothing — and
  the Tekton check then skipped writing *any* result, leaving the row Pending
  on every `benefits-mgmt-app` merge request for weeks. `h2` declares its
  licences inline, which is why the bug looked application-specific.
- **`signature` is an integrity check, not a signature check.** The lab mirror
  publishes no detached GPG signature. The tool says "checksum" and names the
  algorithm. Do not let an agent reword it into "signature verified".

## The merge gate, and why the agent must post a commit status

The Acme app repos now set `only_allow_merge_if_pipeline_succeeds`, and
`report-validation` in the Tekton pipeline exits non-zero unless **every** row
is `passed`. So on the Tekton route a merge request is unmergeable until the
table is green. (Full reasoning: ADR-055 and ADR-056 in `lightwell-workshop`.)

That gate watches the **GitLab pipeline**, and the agent is not part of it. So
the skill now posts an external **commit status** on the MR head SHA:
`pending` before it starts work, `success` or `failed` when it finishes.
GitLab rolls external statuses on a SHA into the pipeline it gates on, which
is what extends the gate to cover the agent.

> **The sharpest edge in this branch.** An agent that dies without closing its
> own `pending` status leaves the merge request **permanently unmergeable**.
> That is worse than a red one, because nothing explains it. If a run has to
> stop early it must post `failed` with a reason first. The skill says this in
> three places; if you change the flow, keep it true.

## What is proven, and what is not

**Proven.** `lw_checks.py` is unit-tested (`tests/test_lw_checks.py`, stdlib
only, runs in the container: `python3 -I tests/test_lw_checks.py`). The
licence and signature logic were additionally exercised against real artifacts
on Maven Central — `commons-text` resolves through two parent hops to
Apache-2.0, `h2` matches on sha512, and a deliberately corrupted jar is
reported as a mismatch rather than passing. The image builds and
`lw_checks.py` runs inside it under the container's Alpine python3.

**Not proven.** No end-to-end run against a live merge request. The image
`sdlc-opencode:pr9` was built on `cluster-sgdql` and deployed to tenant
`t4tdk`, but the AAP job template still needs `verifier_use_opencode: true`
before anything exercises it. **That image predates the `signature` /
`sbom_integrity` / commit-status commits and must be rebuilt before a test
means anything.**

## How to test it

```bash
# 1. rebuild the image from this branch (BuildConfig outputs to :pr9, so the
#    shared sha- tag other tenants use is untouched)
oc start-build sdlc-opencode-pr9 -n lightwell-images --follow

# 2. point one tenant at it, with Argo auto-sync paused so it is not reverted
oc patch app lightwell-tenant-<guid> -n openshift-gitops --type merge \
  -p '{"spec":{"syncPolicy":{"automated":null}}}'
oc set image deploy/opencode \
  opencode=image-registry.openshift-image-registry.svc:5000/lightwell-images/sdlc-opencode:pr9 \
  -n sdlc-<guid>

# 3. switch that tenant's job template to the skill route.
#    opencode_sync_retries matters: the default 90x2s ~= 3 min and an observed
#    run took ~6, so leaving it will time out for the wrong reason.
#    (job template "SDLC Trigger MR Verifier" in org user-<guid>)
curl -sk -u "admin:${AAP_PW}" -X PATCH -H 'Content-Type: application/json' \
  -d '{"extra_vars":"verifier_use_opencode: true\nopencode_sync_retries: 300\n"}' \
  "${AAP}/api/controller/v2/job_templates/<id>/"

# 4. trigger a remediation and watch the table
```

**What success looks like:** all seven keys filled, every one traceable to a
tool invocation in the session log, and the merge request mergeable only once
they are all `passed`. A `warning` is a correct outcome, not a failure to fix
— if the tool could not establish something, the row should say so.

**What to look for specifically**, because these are the ways it has gone
wrong before: a key filled without a corresponding `lw_checks.py` call in the
log; a `warning` upgraded to `passed`; and the agent reporting a stale Job's
result after `oc apply` printed `unchanged` instead of deleting the Job first.

## Open questions for you

1. **`regression` doubles the build.** `module-03` in `lightwell-workshop`
   recorded that this call "has not been made yet". Measured cost on the
   Tekton route: `build-pull-request` averages 43s over 5 runs, so ~86s.
   Natale's position was that 86s is acceptable; the Tekton route now does it.
   If you disagree, drop the baseline build and the `regression` key — nothing
   else depends on it.
2. **Who owns the table.** Both routes write the whole block, and omitted keys
   revert to `Pending`, so running both on one merge request means the last
   writer wins. Either the agent replaces `report-validation`, or it posts
   only a commit status and leaves the table to Tekton. This branch assumes
   the former.
3. **Duplication.** The licence and SBOM logic now exists in `lw_checks.py`
   *and* in the heredoc inside `.tekton/build-pull-request.yaml` across three
   app repos — four copies. Tonight's licence fix was a four-way edit. If the
   skill route is the destination, having Tekton call this same script is
   worth doing before the surface grows again.
