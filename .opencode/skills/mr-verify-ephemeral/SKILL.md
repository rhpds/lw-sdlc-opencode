---
name: mr-verify-ephemeral
description: Parse MR handoff, run isolated verify Job, deploy ephemeral environment, smoke test, and post GitLab MR note via gitlab_api.py.
---

# MR verify ephemeral

Automates **Agent 2** behavior (`spec.md` §5). GitLab reads/notes via **`python3 /app/scripts/gitlab_api.py`** — see `.opencode/reference/gitlab-mcp.md`. The `license`, `regression`, `signature` and `sbom_integrity` rows of the validation table are measured by **`python3 /app/scripts/lw_checks.py`** (step 5), not judged.

## Inputs

Session JSON (from EDA / GitLab webhook normalization):

```json
{
  "merge_request_iid": 67,
  "project_id": 12345,
  "source_branch": "update-artifact-2.17.1",
  "repository_git_url": "https://gitlab.example.com/group/repo.git"
}
```

## Preconditions

1. Load only this skill—not `dependency-impact-remediation`.
2. `GITLAB_PAT` available; on CLI `401` stop — see `.opencode/reference/gitlab-credentials.md`.
3. `oc` / `kubectl` available and authenticated as verifier ServiceAccount.

## Step 1 — Parse handoff (fail closed)

1. Fetch MR:
   ```bash
   python3 /app/scripts/gitlab_api.py mr-get --project-id <project_id> --mr-iid <merge_request_iid>
   ```
2. From `description`, locate `<!-- agent-handoff: do not edit below -->` and parse the following fenced `json` block.
3. Validate required keys: `artifact_id`, `new_version`, `source_branch`, `target_branch`, `project_id`, `merge_request_iid`.
4. If missing or invalid JSON:
   ```bash
   python3 /app/scripts/gitlab_api.py mr-note --project-id <id> --mr-iid <iid> \
     --body "Verifier blocked: missing or invalid agent-handoff JSON."
   ```
   **Stop**—do not create Jobs or namespaces.

Prefer handoff JSON over webhook fields when both exist; they must agree on `merge_request_iid` and `source_branch`.

## Step 2 — Compile and unit test (isolated Job)

**Forbidden:** Running `mvn clean verify`, `gradle test`, or `npm test` inside the OpenCode control-plane container.

Demo depth **A** (`lw-demo-help-app`): use **`mvn clean verify`** unless handoff specifies otherwise.

The Job runs the build **twice** — once on the MR branch, once with the target
branch's `pom.xml` restored. The second run is the baseline the `regression`
check in step 5 compares against; without it that check has nothing to measure
and the agent is left inventing a behavioural diff. Budget roughly double the
build time.

1. **Idempotency — always delete before apply.** A Job spec is immutable, so
   `oc apply` over an existing Job prints `job.batch/verify-mr-<iid> unchanged`
   and changes nothing. If a previous Job for this MR failed, the poll in step 5
   then reads *that* Job's terminal state and you report a stale result as if it
   were current. Do not branch on whether a Job exists — unconditionally:

   ```bash
   oc delete job verify-mr-<merge_request_iid> -n "$SDLC_SANDBOX_NAMESPACE" --ignore-not-found --wait=true
   oc apply -f /tmp/opencode/verify-job-mr-<merge_request_iid>.yaml
   ```

   Then confirm you are looking at the new Job before trusting its logs:
   `oc get job verify-mr-<iid> -n "$SDLC_SANDBOX_NAMESPACE" -o jsonpath='{.metadata.creationTimestamp}'`
   must be from this run. If the apply printed `unchanged`, **stop** — you are
   about to report someone else's result.

2. **Provision git credentials in `$SDLC_SANDBOX_NAMESPACE`.** The clone target is a private GitLab project and this namespace does **not** inherit the control-plane PAT, so a Job without credentials dies on `fatal: could not read Username`. Create the secret idempotently before applying the Job:

   ```bash
   oc create secret generic gitlab-pat -n "$SDLC_SANDBOX_NAMESPACE" \
     --from-literal=token="$GITLAB_PAT" \
     --dry-run=client -o yaml | oc apply -f -
   ```

3. **Point Maven at the tenant Nexus.** The remediated artifact under test (`<new_version>`, e.g. `6.0.3.rhlw-00001`) exists **only** in the tenant's Lightwell proxy — it is not on Maven Central. A Job without this settings file fails with `Could not find artifact ... in central`, which looks like a broken bump but is really a misconfigured build.

   Discover the tenant's Nexus, then publish the settings as a ConfigMap:

   ```bash
   # These are injected into the OpenCode pod by the bootstrap-tenant chart.
   # Do NOT rediscover them with `oc get cm tenant-integration -A`: there are
   # two such ConfigMaps per tenant, and items[0] can resolve a DIFFERENT
   # tenant on a shared cluster.
   NEXUS_NS="$NEXUS_NAMESPACE"
   NEXUS_REPOS="$NEXUS_REPOSITORIES"
   NEXUS_BASE="http://nexus.${NEXUS_NS}.svc.cluster.local:8081/repository"
   ```

   Use the in-cluster Service over plain HTTP, **not** the public route — the route serves a self-signed certificate that Maven rejects.

   Write `/tmp/opencode/settings.xml` with one `<repository>` per entry in `NEXUS_REPOS` (comma-separated; the `remediated` one carries the fix), then:

   ```bash
   oc create configmap maven-settings -n "$SDLC_SANDBOX_NAMESPACE" \
     --from-file=settings.xml=/tmp/opencode/settings.xml \
     --dry-run=client -o yaml | oc apply -f -
   ```

   Maven 3.8.1+ ships a built-in `maven-default-http-blocker` mirror that refuses every plain-HTTP repository, so the Nexus URLs above fail with `Blocked mirror for repositories: [...]` unless that blocker is overridden. Redefining a mirror with the same id and `<mirrorOf>dummy</mirrorOf>` replaces it. This is safe here and only here: the traffic is pod-to-Service inside the cluster and never crosses the pod network. Do **not** carry this override into any build that resolves over the public internet.

   ```xml
   <settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
     <mirrors>
       <mirror>
         <id>maven-default-http-blocker</id>
         <mirrorOf>dummy</mirrorOf>
         <name>Allow in-cluster HTTP to Nexus</name>
         <url>http://0.0.0.0/</url>
       </mirror>
     </mirrors>
     <profiles>
       <profile>
         <id>lightwell</id>
         <repositories>
           <!-- one block per NEXUS_REPOS entry, remediated first -->
           <repository>
             <id>redhat-packages-remediated</id>
             <url>http://nexus.NEXUS_NS.svc.cluster.local:8081/repository/redhat-packages-remediated-GUID/</url>
             <releases><enabled>true</enabled></releases>
             <snapshots><enabled>false</enabled></snapshots>
           </repository>
         </repositories>
       </profile>
     </profiles>
     <activeProfiles><activeProfile>lightwell</activeProfile></activeProfiles>
   </settings>
   ```

   Leave Maven Central as the implicit fallback — do **not** add a catch-all `<mirror>`, or ordinary dependencies stop resolving when the proxy lacks them.

4. **Write the Job manifest to a file, then apply it** — `oc apply -f /tmp/opencode/verify-job-mr-<iid>.yaml`. Do not pipe a heredoc into `oc apply -f -`.

   Use the manifest below verbatim, substituting only the `<...>` placeholders. Three constraints it encodes are not optional:

   - **Writable `HOME`.** The Job runs under the `restricted-v2` SCC as an arbitrary UID, so `/` and the image's default `$HOME` are read-only. Without both `HOME` and `-Dmaven.repo.local` pointed at the emptyDir, Maven fails immediately with `Could not create local repository at /.m2/repository`.
   - **`-s /settings/settings.xml`.** Without it Maven never reaches the tenant Nexus and cannot resolve the remediated artifact (step 3).
   - **`backoffLimit: 0`.** Retries multiply the log output the MR note has to summarize without changing the outcome.
   - **`BASE_BRANCH`, and the `===LW-…===` markers around each build.** Step 5 locates the two runs in the log by those markers; drop them and the `regression` check has nothing to parse. The baseline `git fetch` works because the init container cloned with the PAT in the remote URL — the build container needs no credentials of its own.

   ```yaml
   apiVersion: batch/v1
   kind: Job
   metadata:
     name: verify-mr-<merge_request_iid>
     namespace: ${SDLC_SANDBOX_NAMESPACE}
     labels:
       mr-iid: "<merge_request_iid>"
   spec:
     backoffLimit: 0
     ttlSecondsAfterFinished: 3600
     template:
       metadata:
         labels:
           mr-iid: "<merge_request_iid>"
       spec:
         restartPolicy: Never
         volumes:
           - name: workspace
             emptyDir: {}
           - name: settings
             configMap:
               name: maven-settings
         initContainers:
           - name: checkout
             image: registry.access.redhat.com/ubi9/toolbox:latest
             command: ["/bin/sh", "-c"]
             args:
               - git clone --depth 1 --branch "$GIT_BRANCH"
                 "https://oauth2:${GITLAB_PAT}@${GIT_HOST_PATH}" /workspace/src
             env:
               - name: GIT_BRANCH
                 value: "<source_branch>"
               # repository_git_url with the https:// scheme stripped
               - name: GIT_HOST_PATH
                 value: "<host/group/repo.git>"
               - name: GITLAB_PAT
                 valueFrom:
                   secretKeyRef:
                     name: gitlab-pat
                     key: token
             volumeMounts:
               - name: workspace
                 mountPath: /workspace
         containers:
           - name: build
             image: registry.access.redhat.com/ubi9/openjdk-17:latest
             workingDir: /workspace/src
             command: ["/bin/sh", "-c"]
             args:
               - |
                 set -u
                 M="mvn -B clean verify -s /settings/settings.xml -Dmaven.repo.local=/workspace/.m2/repository"
                 echo "===LW-HEAD==="
                 $M; rc=$?
                 # The CycloneDX SBOM this build produced. Emit it now: the
                 # baseline run below starts with `clean` and deletes target/,
                 # and the pod is gone before anyone can read the workspace.
                 echo "===LW-SBOM==="
                 grep -o 'pkg:maven/[^"]*' target/bom.json 2>/dev/null | sort -u
                 # Baseline: the same test code against the pre-bump pom.xml.
                 # Only pom.xml is restored, so any difference in outcomes is
                 # attributable to the dependency version and nothing else.
                 echo "===LW-BASE==="
                 if git fetch --depth 1 origin "$BASE_BRANCH" \
                    && git checkout FETCH_HEAD -- pom.xml; then
                   $M || true
                 else
                   echo "baseline checkout failed"
                 fi
                 echo "===LW-END==="
                 # Exit on the HEAD build only. A red baseline is information,
                 # not a reason to call this MR broken.
                 exit $rc
             env:
               - name: HOME
                 value: /workspace
               - name: BASE_BRANCH
                 value: "<target_branch>"
             volumeMounts:
               - name: workspace
                 mountPath: /workspace
               - name: settings
                 mountPath: /settings
                 readOnly: true
   ```

   Add `runtimeClassName: kata` to `spec.template.spec` when the cluster provides it.

5. **Wait for either terminal condition.** `oc wait --for=condition=complete` never returns on a Job that failed — it blocks for the whole timeout, so the verifier reports "blocked" when it should report "failed". Poll for both:

   ```bash
   for _ in $(seq 1 150); do
     s="$(oc get job verify-mr-<merge_request_iid> -n "$SDLC_SANDBOX_NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status} {.status.conditions[?(@.type=="Failed")].status}')"
     case "$s" in "True"*) echo COMPLETE; break ;; *"True") echo FAILED; break ;; esac
     sleep 10
   done
   oc logs job/verify-mr-<merge_request_iid> -n "$SDLC_SANDBOX_NAMESPACE" \
     --all-containers > /tmp/opencode/verify-mr-<merge_request_iid>.log
   tail -200 /tmp/opencode/verify-mr-<merge_request_iid>.log
   ```

   Save the whole log to the file — do not use `--tail` for the saved copy.
   Step 5 reads the baseline build out of it, and that section is near the
   middle of a two-build log, not the end.

6. Non-zero exit → **still do step 5 first**, then post the MR note with a log
   excerpt via `mr-note`, then **stop** (no ephemeral deploy).

   Skipping step 5 on failure is not the safe option. The table may already
   carry passing rows from an earlier run of another verifier; leaving it
   untouched publishes a green table for a build you just watched fail. Send
   the keys the failure actually speaks to — for a dependency-resolution error
   that is `dependency_compat: failed`, and `unit_tests` stays **omitted**,
   because a build that died before Surefire ran did not fail its unit tests,
   it never had any. `mr-validation` rewrites Recommended Action to
   "Do not merge" on any `failed` key.

## Step 3 — Ephemeral deploy

**Applicability check — do this before creating anything.** The Dockerfile and
smoke test below assume an executable Spring Boot **jar** (demo depth A,
`lw-demo-help-app`). Check the packaging in the MR branch's `pom.xml`:

```bash
python3 /app/scripts/gitlab_api.py file-get --path <gitlab_path> --file-path pom.xml --ref <source_branch> | grep -m1 '<packaging>'
```

If it is anything other than `jar` — `war`, for instance — this skill cannot
deploy it: `java -jar app.war` fails at once with `no main manifest attribute`
and no number of rebuilds will change that. Skip steps 3 and 4, leave the
smoke-test-dependent rows Pending, and say so in the step 6 note. Steps 2 and 5
are packaging-independent and still apply.

Namespace: `${EPHEMERAL_NS_PREFIX}-<merge_request_iid>`.

1. **Idempotency:** if the namespace exists, inspect the existing BuildConfig/Deployment/Route before creating duplicates. A BuildConfig can be re-run with `oc start-build`; it does not need recreating.
2. `oc create namespace ${EPHEMERAL_NS_PREFIX}-<merge_request_iid>`.
3. **Provision git credentials again — as a `basic-auth` secret this time.** The ephemeral namespace is separate from `$SDLC_SANDBOX_NAMESPACE`, so it does not have the step 2 secret, and an OpenShift build will **not** accept the Opaque `gitlab-pat`: `source.sourceSecret` requires type `kubernetes.io/basic-auth`. Without it the build pod dies in its init container with `could not read Username`, exactly as the verify Job does in step 2.

   ```bash
   oc create secret generic gitlab-basic -n ${EPHEMERAL_NS_PREFIX}-<merge_request_iid> \
     --type=kubernetes.io/basic-auth \
     --from-literal=username=oauth2 --from-literal=password="$GITLAB_PAT" \
     --dry-run=client -o yaml | oc apply -f -
   ```

4. **Build the MR branch with an inline Dockerfile.** `lw-demo-help-app` ships **no Dockerfile**, so a plain `--strategy=docker` build against the git URL fails in about ten seconds with `ManageDockerfileFailed` / `open /tmp/build/inputs/Dockerfile: no such file or directory` — before it ever compiles anything. Supply the Dockerfile through `--dockerfile` instead; the git source still provides the application code.

   The image must be built the same way the step 2 Job was: a multi-stage build whose builder stage resolves through the **tenant Nexus**, or Maven cannot find the remediated `.rhlw-` artifact. Reuse the `NEXUS_NS` / `NEXUS_REPOS` values discovered in step 3 of the verify Job, and the same `maven-default-http-blocker` override.

   Write the Dockerfile to a file first, substituting the discovered Nexus values into the embedded `settings.xml`:

   ```dockerfile
   FROM registry.access.redhat.com/ubi9/openjdk-17:1.20 AS builder
   USER 0
   WORKDIR /build
   COPY pom.xml .
   COPY src ./src
   # One <repository> per NEXUS_REPOS entry, remediated first; keep the blocker override.
   RUN mkdir -p /root/.m2 && printf '%s' '<settings ...>' > /root/.m2/settings.xml
   RUN mvn -B clean package -DskipTests
   FROM registry.access.redhat.com/ubi9/openjdk-17-runtime:1.20
   COPY --from=builder /build/target/help-im-vulnerable-*.jar /deployments/app.jar
   COPY --from=builder /build/target/bom.json /deployments/maven-sbom.json
   EXPOSE 8080
   ENTRYPOINT ["java", "-jar", "/deployments/app.jar"]
   ```

   ```bash
   oc new-build --name=help-im-vulnerable --strategy=docker \
     --source-secret=gitlab-basic \
     --dockerfile="$(cat /tmp/opencode/Dockerfile.mr-<merge_request_iid>)" \
     "<repository_git_url>#<source_branch>" \
     --to=help-im-vulnerable:mr-<merge_request_iid> \
     -n ${EPHEMERAL_NS_PREFIX}-<merge_request_iid>
   ```

   Confirm both `.spec.source.sourceSecret.name` and `.spec.source.dockerfile` are set before starting the build; `oc new-build` silently omits the secret if it does not yet exist. A started build reports its source as `Dockerfile,Git@<sha>` when the inline Dockerfile took effect — plain `Git@<sha>` means it did not, and the build will fail.

5. Deploy the built image from the internal registry and expose it. The container listens on **8080** and `oc new-app` names that port `8080-tcp`, so `--port=http` does not match any port on the Service and route creation fails:

   ```bash
   oc new-app --image-stream=help-im-vulnerable:mr-<merge_request_iid> -n ${EPHEMERAL_NS_PREFIX}-<merge_request_iid>
   oc create route edge --service=help-im-vulnerable --port=8080 -n ${EPHEMERAL_NS_PREFIX}-<merge_request_iid>
   ```

6. Wait for the rollout before smoke testing, and record the Route URL. **Always
   pass `--timeout`** — bare `oc rollout status` blocks until the Deployment's
   `progressDeadlineSeconds` (default 600s) and, on an image that crashes on
   startup, it will simply keep the session busy:

   ```bash
   oc rollout status deploy/<app> -n ${EPHEMERAL_NS_PREFIX}-<merge_request_iid> --timeout=300s
   ```

   If it times out, do **not** delete and recreate the Deployment to try again —
   a crash-looping pod is a result, not a transient error. Capture
   `oc logs deploy/<app> --tail=50`, report the smoke test as `failed` with that
   excerpt, and move to step 5.

Platform namespaces for OpenCode are managed by **Argo CD**—do not modify `sdlc-control-plane` or `sdlc-mcp-servers` in this skill.

## Step 4 — Smoke tests

Run the checks from a pod inside the cluster, not from the OpenCode container — the Route's edge certificate is signed by the cluster CA and the demo app is only reachable on the cluster network.

```bash
oc run smoke-mr-<merge_request_iid> --rm -i --restart=Never \
  -n ${EPHEMERAL_NS_PREFIX}-<merge_request_iid> \
  --image=registry.access.redhat.com/ubi9/toolbox:latest -- \
  /bin/sh -c "curl -ksS --fail --max-time 30 https://<route-host>/api/status"
```

For demo depth **A**, `/api/status` returns one row per tracked library. The bump under test is verified when the row for the remediated `artifact_id` reports the new `.rhlw-` version in `lightwellFix` with `"loaded": true`. Capture pass/fail and a short response snippet (no secrets) — do not paste the whole payload into the MR note.

## Step 5 — Fill the Automated Validation table

The MR body created by `bump-maven-mr` carries an **Automated Validation**
table with every row Pending. You own those rows: replace them with what the
verify Job and smoke tests actually produced.

### Which checks this skill can actually establish

Seven keys. Three you read out of the Job log; four you **measure with
`lw_checks.py`**, which exists because reasoning about them from a green build
is guessing:

| Key | Evidence you must be able to quote |
|-----|------------------------------------|
| `unit_tests` | the Surefire `Tests run: N, Failures: …, Errors: …, Skipped: …` summary line |
| `integration_tests` | the Failsafe `Tests run: …` summary line |
| `dependency_compat` | `BUILD SUCCESS`, or the resolution error that replaced it |
| `regression` | the JSON printed by `lw_checks.py regression` |
| `license` | the JSON printed by `lw_checks.py license` |
| `signature` | the JSON printed by `lw_checks.py signature` |
| `sbom_integrity` | the JSON printed by `lw_checks.py sbom` |

There is no longer any key this skill cannot establish. That does **not** make
it acceptable to fill a row you did not run the tool for: sending a key because
a green table looks finished is the one failure this step exists to prevent.
If a tool failed to run, omit its key and say so in the step 6 note.

#### Measure `regression`

The two-build verify Job ran the same test code against the pre-bump and
post-bump `pom.xml`. Compare the two runs:

```bash
python3 /app/scripts/lw_checks.py regression --log /tmp/opencode/verify-mr-<iid>.log
```

#### Measure `license`

You need the group, the artifact, and the version the MR is bumping **from**.
The handoff JSON carries the artifact and the new version; read the old version
out of the target branch's pom:

```bash
python3 /app/scripts/gitlab_api.py file-get --path <gitlab_path> \
  --file-path pom.xml --ref <target_branch>
```

Then compare the licences the two versions resolve to:

```bash
python3 /app/scripts/lw_checks.py license \
  --nexus-base "http://nexus.${NEXUS_NAMESPACE}.svc.cluster.local:8081" \
  --group <group_id> --artifact <artifact_id> \
  --old-version <old_version> --new-version <new_version> \
  --repo "$NEXUS_REPOSITORIES"
```

`--nexus-base` takes the host and port **without** `/repository` — the script
appends `/repository/<repo>/...` itself. `--repo` and `--proxy` both accept a
comma-separated list and every POM fetch tries each in turn, so pass all the
candidates rather than guessing which one holds the artifact.

Two things the script handles that a manual reading would get wrong, and which
are why you must not shortcut it:

- **Most artifacts declare no licence of their own.** `commons-text` inherits
  Apache-2.0 from a grandparent POM; reading only its own POM says "no licence
  declared" for the dependency this whole lab is built around. The script walks
  the `<parent>` chain the way Maven does.
- **One licence has many spellings.** `Apache-2.0`, `Apache License, Version
  2.0` and `apache2-something` are the same licence, and `MPL 2.0 or EPL 1.0`
  is the same pair as two separate `<license>` elements. The script normalises
  to SPDX ids before comparing, so it reports a change only when the licence
  genuinely changed.

#### Measure `signature` and `sbom_integrity`

Both need the same coordinate as the licence check.

```bash
python3 /app/scripts/lw_checks.py signature \
  --nexus-base "http://nexus.${NEXUS_NAMESPACE}.svc.cluster.local:8081" \
  --group <group_id> --artifact <artifact_id> --version <new_version> \
  --repo "$NEXUS_REPOSITORIES"

python3 /app/scripts/lw_checks.py sbom --log /tmp/opencode/verify-mr-<iid>.log \
  --group <group_id> --artifact <artifact_id> --version <new_version>
```

`signature` downloads the published JAR and compares it against the checksum
the artifact manager recorded beside it, preferring the strongest digest
available. Despite the row name this is an **integrity** check — the lab mirror
publishes no detached GPG signature — and the tool's own wording says so. Do
not reword it into "signature verified".

`sbom` asserts that the CycloneDX SBOM this build produced lists the bumped
coordinate, reading the purls out of the `===LW-SBOM===` section of the Job
log. A `failed` here usually means the build resolved the *old* version, which
is a genuine finding about the MR and not a tooling problem — the details
string will name the version it actually found.

All four commands print `{"status": ..., "details": ...}` — pass that object through
to `mr-validation` as-is. Do not rewrite a `warning` into a `passed` because the
build was green: a `warning` means the tool could not establish something, and
saying so is the measurement.

#### Send the results

A passing run sends all seven keys:

```bash
python3 /app/scripts/gitlab_api.py mr-validation --project-id <id> --iid <iid> --results '{
  "unit_tests":        {"status": "passed", "details": "<N>/<N> tests passed (0 failures, 0 errors, 0 skipped)"},
  "integration_tests": {"status": "passed", "details": "<N>/<N> tests passed (0 failures, 0 errors, 0 skipped)"},
  "dependency_compat": {"status": "passed", "details": "Build succeeded against <artifact_id>:<new_version>"},
  "regression":        <paste the lw_checks.py regression object>,
  "license":           <paste the lw_checks.py license object>,
  "signature":         <paste the lw_checks.py signature object>,
  "sbom_integrity":    <paste the lw_checks.py sbom object>
}'
```

Sending all seven matters beyond completeness: **any key you omit renders as
`Pending` and erases whatever was in that row before**, so a partial run can
silently wipe results another route already measured.

Every `<N>` above is a placeholder. Copying a number out of this file instead of
reading it out of the Job log is a fabrication even when the build did pass. The
same applies to the four `lw_checks.py` objects: paste what the command printed,
not what this example suggests it might print.

Rules, in order of importance:

1. **Report only what you measured.** Before you send a key, name the line of
   Job output, smoke-test output, or `lw_checks.py` JSON it came from. If you
   cannot quote that line, or if the justification is "inferred from the build
   passing", the check was not measured: leave the key out. Inference is not
   measurement — a build that compiles tells you nothing about licence metadata
   or behavioural regression, which is precisely why those two now have a tool
   rather than a judgement call. A fabricated ✅ is the worst possible outcome
   here, because the whole point of the table is that a human can trust it.
2. `status` is one of `passed`, `failed`, `pending`, `skipped`, `warning`.
3. On any failure use `"status": "failed"` with the real reason. The command
   rewrites **Recommended Action** to "Do not merge" by itself — do not also
   edit the description by hand.
4. Only pass `--ready` (which drops the `Draft:` prefix) when every check
   passed *and* the risk class does not require human review. On the AO path
   leave the MR as draft; AO handles merging.
5. The command rewrites only the marked block, so the Summary and Vulnerability
   Details written at creation time are preserved. Never use `mr-update` to
   replace the whole description.

## Step 6 — Summarize on MR

```bash
python3 /app/scripts/gitlab_api.py mr-note --project-id <id> --mr-iid <iid> --body "<markdown summary>"
```

Include: verify Job name and pass/fail, ephemeral namespace and Route URL, smoke result, TTL reminder.

## Failure handling

| Condition | Action |
|-----------|--------|
| Invalid handoff | `mr-note`; stop |
| Job failed | step 5 with the failing key(s), then `mr-note` with logs; stop |
| `oc apply` prints `job.batch/... unchanged` | You are about to read a previous run's Job. Delete it and re-apply (step 2.1); never poll a Job you did not create |
| Deployment never becomes ready | A crash-looping pod is a result. Report the smoke test `failed` with `oc logs` output — do not delete and recreate the Deployment to retry |
| Tempted to write any of `regression`, `license`, `signature`, `sbom_integrity` from judgement | Run `lw_checks.py`. If it returned a `warning`, send the warning — do not upgrade it because the build was green |
| `lw_checks.py sbom` → `lists <artifact> at <old version>` | A real finding about the MR: the build resolved the pre-bump version. Report it `failed`; do not re-run hoping for a different answer |
| `lw_checks.py signature` → `warning: published no checksum` | The artifact manager recorded no digest. Send the warning — "downloaded fine" is not integrity |
| `lw_checks.py license` → `could not read the POM ... from any of` | A repo-name guess, not a bad MR. Check `NEXUS_REPOSITORIES` and pass every candidate to `--repo`/`--proxy` before reporting anything |
| `lw_checks.py regression` → `no ===LW-HEAD=== section` | The Job ran the single-build command. Fix the Job args (step 2.4) and re-run; do not hand-write the key |
| Job still running after the poll budget | `mr-note` reporting a timeout — never report a failed Job as "blocked" |
| `Could not find artifact ... in central` | Build config bug, not a bad bump — check the `maven-settings` ConfigMap and `-s` flag before blaming the MR |
| `Blocked mirror for repositories` | The `maven-default-http-blocker` override is missing from settings.xml |
| Build pod `Init:Error` with `could not read Username` | The BuildConfig has no `sourceSecret`, or it points at an Opaque secret — step 3 needs a `kubernetes.io/basic-auth` one |
| Build `Failed (ManageDockerfileFailed)` in ~10s, `open /tmp/build/inputs/Dockerfile: no such file or directory` | The repo has no Dockerfile — pass one with `--dockerfile`; do not retry the build unchanged |
| Ephemeral build fails with `Could not find artifact ... in central` | The inline Dockerfile's `settings.xml` is missing the tenant Nexus repos — same root cause as step 2, different file |
| `oc create route` rejects `--port=http` | The Service port is `8080-tcp`; use `--port=8080` |
| `oc` forbidden | `mr-note`; stop |
| Duplicate webhook | Prefer idempotent Job/NS checks before create |

## Completion

Confirm the MR note was posted; return Job name, namespace, and Route URL.
