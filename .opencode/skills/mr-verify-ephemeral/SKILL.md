---
name: mr-verify-ephemeral
description: Parse MR handoff, run isolated verify Job, deploy ephemeral environment, smoke test, and post GitLab MR note via gitlab_api.py.
---

# MR verify ephemeral

Automates **Agent 2** behavior (`spec.md` §5). GitLab reads/notes via **`python3 /app/scripts/gitlab_api.py`** — see `.opencode/reference/gitlab-mcp.md`.

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

1. **Idempotency:** `oc get job -n "$SDLC_SANDBOX_NAMESPACE" -l mr-iid=<merge_request_iid>` — if a running/succeeded Job exists, reuse logs or skip recreate per operator policy. If a **failed** Job exists for this MR, delete it first (`oc delete job -n "$SDLC_SANDBOX_NAMESPACE" -l mr-iid=<merge_request_iid>`); a Job spec is immutable, so re-applying over a failed one silently keeps the old spec.

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
               - mvn -B clean verify -s /settings/settings.xml
                 -Dmaven.repo.local=/workspace/.m2/repository
             env:
               - name: HOME
                 value: /workspace
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
   for _ in $(seq 1 90); do
     s="$(oc get job verify-mr-<merge_request_iid> -n "$SDLC_SANDBOX_NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status} {.status.conditions[?(@.type=="Failed")].status}')"
     case "$s" in "True"*) echo COMPLETE; break ;; *"True") echo FAILED; break ;; esac
     sleep 10
   done
   oc logs job/verify-mr-<merge_request_iid> -n "$SDLC_SANDBOX_NAMESPACE" --all-containers --tail=200
   ```

6. Non-zero exit → post MR note with log excerpt via `mr-note`; **stop** (no ephemeral deploy).

## Step 3 — Ephemeral deploy

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

6. Wait for `oc rollout status deploy/help-im-vulnerable` before smoke testing, and record the Route URL.

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

## Step 5 — Summarize on MR

```bash
python3 /app/scripts/gitlab_api.py mr-note --project-id <id> --mr-iid <iid> --body "<markdown summary>"
```

Include: verify Job name and pass/fail, ephemeral namespace and Route URL, smoke result, TTL reminder.

## Failure handling

| Condition | Action |
|-----------|--------|
| Invalid handoff | `mr-note`; stop |
| Job failed | `mr-note` with logs; stop |
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
