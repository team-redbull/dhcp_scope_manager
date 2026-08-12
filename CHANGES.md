# Single-source DHCP API token — changes to apply in the air-gapped repos

Everything done in this change set, per repo, in apply order. Most of it is a
straight port: the GitHub-side commits are the reference, and only the items marked
**AIR-GAPPED ONLY** have no counterpart to copy from.

**What this fixes.** The DHCP API bearer token existed in three places and was
authored in two repos:

| Where (before) | Namespace | Authored in | After |
| --- | --- | --- | --- |
| mgmt cluster | `dhcp-scope-manager` | `oc create secret` out of band | **kept**, now rendered from one committed value |
| mgmt cluster | `redbull-workflows` | out of band, "must stay in sync" | **deleted** — its only consumer was a GET, and GETs are anonymous now |
| every MCE | `hcp-<cluster>` × N | `hostedclusters-setup`, once per hosted cluster | **one per MCE**, in `dhcp-scope-manager` |

After this, rotation is one commit in one file plus one `oc rollout restart`.

---

## ⚠️ Apply order

The order matters in three places. Everything else can land in any sequence.

1. **`dhcp_scope_manager` (the API image) first.** It makes the scope `GET`s
   anonymous. Nothing else may land before the new image is actually running.
2. **`helm-charts/dhcp-scope-manager` + `gitops-day2-prod` next** — this is what puts
   one token Secret on the mgmt cluster and on every MCE.
3. **`helm-charts/hostedclusters-setup` after that.** It repoints every Request at the
   shared Secret. Landing it before step 2 means every scope *write* 401s until the
   Secret arrives. (Reads keep working — they need no token.)
4. **`workflows` + `helm-charts/segment-lifecycle-worker` only after step 1 is live.**
   They delete the worker's token; before anonymous GET is deployed, its convergence
   poll would 401 on every attempt.

Steps 2 and 4 are safe to do in either order relative to each other.

### Before you start

The token Secret is not deployed on any MCE today and the air-gapped API is not
production, so there is nothing to preserve and no live 401 window to protect.
**Generate a fresh token** rather than copying the one from the GitHub copy — that
one is public.

```sh
LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48; echo
```

The two copies of `helm-charts/dhcp-scope-manager` hold **different** tokens by
design. Never sync that line in either direction.

---

## Repo 1: `dhcp_scope_manager` — anonymous scope GETs, test runner removed

Port the commits as-is. Two independent changes ship together here.

### a. Scope `GET`s become unauthenticated

`app/routers/scopes.py` splits its one router in two. Writes keep `verify_token`;
reads drop it. Two routers rather than per-route dependencies, because FastAPI
cannot subtract a router-level dependency from a single route — and this way the
safe case is the default: a route added to `router` inherits auth, and anonymity has
to be chosen deliberately.

```diff
+router = APIRouter(
+    prefix="/api/v1", tags=["scopes"],
+    dependencies=[Depends(verify_token), Depends(require_dhcp_service)],
+)
+
+read_router = APIRouter(
+    prefix="/api/v1", tags=["scopes"],
+    dependencies=[Depends(require_dhcp_service)],
+)
```

Both `GET` handlers move to `read_router`; `app/routers/__init__.py` includes both.
`require_dhcp_service` stays on reads — a `GET` still talks to the DHCP server.

**Why:** `segment-lifecycle-worker`'s `allocate_segment` polls
`GET /api/v1/scopes/{network}` to confirm Crossplane converged. Behind auth, that
poll needed the token in its own namespace (Secrets are namespace-scoped and
`envFrom` resolves per-pod), so a second copy lived in `redbull-workflows` and had to
rotate in step with the first. Opening reads deleted that copy.

**What it costs, and it is real:** a `GET` returns the full scope state — mask,
ranges, gateway, DNS servers, DNS domain, PXE boot server, exclusions, failover
partner hostname — and `GET /api/v1/scopes` returns every scope at once. Crossplane
reaches the API through the OpenShift Route in this environment, so the Route cannot
be turned off as a mitigation: **the whole addressing plan is readable by anything
that can reach that Route.** Writes stay authenticated, so integrity is unchanged.

Pinned by `test_route_auth_matrix`, which asserts the exact set of anonymous routes
so a new route cannot join them by accident.

### b. `POST /api/v1/test-runs` and everything behind it, deleted

**This is the one that changes how you test air-gapped.** The deployed API can no
longer run its own pytest suite. Deleted: `app/routers/testrunner.py`,
`app/services/test_runner.py`, `app/models/test_run.py`, three `ErrorCode` members
and their error classes, `TEST_RUNNER_*` settings, `tests/test_testrunner.py`.

Three supporting changes went with it, and **all three have to be reverted together**
if the endpoint is ever wanted back:

- `requirements.txt` no longer carries `pytest` / `pytest-asyncio` / `httpx`; they
  moved to a new `requirements-dev.txt`.
- The `Dockerfile` no longer does `COPY tests/` or `COPY scripts/`.
- `.github/workflows/test.yml` installs `requirements-dev.txt` as well. **If the
  air-gapped side builds the image or runs the suite through its own pipeline,
  that pipeline needs the same `-r requirements-dev.txt`**, or collection fails.

Run the suite from a checkout instead. `tests/integration/` still self-skips unless
`DHCP_IT=1`.

---

## Repo 2: `helm-charts/dhcp-scope-manager` — the token subchart

### a. NEW: `charts/dhcp-api-token/`

Three files. This subchart is the single producer of the token, in every namespace
on every cluster.

`charts/dhcp-api-token/Chart.yaml` — `apiVersion: v2`, `name: dhcp-api-token`,
`type: application`, `version: 0.1.0`.

`charts/dhcp-api-token/values.yaml` — **the only file a rotation edits**:

```yaml
token: "<the air-gapped token — NOT the one in the GitHub copy>"
```

`charts/dhcp-api-token/templates/token.yaml`:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: dhcp-scope-manager-token
  labels:
    app.kubernetes.io/name: dhcp-api-token
    app.kubernetes.io/managed-by: {{ .Release.Service }}
type: Opaque
stringData:
  api-token: {{ required "dhcp-api-token: values.yaml has no token. ..." .Values.token | quote }}
```

Three things about this that are load-bearing:

- **The Secret name and key are constants, not values.** Two other places already
  spell them out (`apiAuth.secretName`/`tokenKey` here, `dhcp_api.tokenSecretRef` in
  `hostedclusters-setup`). A third configurable copy would be a third thing to keep
  in step, and a mismatch is not a render error — it is a placeholder provider-http
  passes through as literal text, i.e. a 401 naming nothing.
- **No namespace is stamped.** Rendered standalone on an MCE the Secret lands in the
  Application's destination namespace; rendered as a subchart on the mgmt cluster it
  lands in the release namespace. Both are `dhcp-scope-manager`. Stamping one would
  break the standalone case — and it is also why nothing here is cluster-scoped.
- **`required` is the whole guard.** An absent token fails the render rather than
  producing a Secret nobody can authenticate with.

### b. DELETE `templates/secret.yaml`

The `lookup`-then-`randAlphaNum` generator. Argo renders with `helm template`, where
`lookup` returns nothing, so **every sync minted a new token while the running pod
kept its startup value.** Nothing replaces it.

### c. `apiAuth.existingSecret` → `apiAuth.secretName` (breaking values key)

The chart creates the Secret now, so "existing" actively misleads. Change in
`values.yaml`, `templates/deployment.yaml`, and every values file that sets it — see
repo 6. **Leave `winrm.existingSecret` alone**: that one genuinely is created out of
band, and after this rename the two names finally mean different things.

`templates/deployment.yaml` loses its conditional:

```diff
-                  {{- if .Values.apiAuth.existingSecret }}
-                  name: {{ .Values.apiAuth.existingSecret }}
-                  key: {{ .Values.apiAuth.tokenKey }}
-                  {{- else }}
-                  name: {{ include "dhcp-scope-manager.fullname" . }}-api-token
-                  key: api-token
-                  {{- end }}
+                  name: {{ .Values.apiAuth.secretName }}
+                  key: {{ .Values.apiAuth.tokenKey }}
```

### d. Declare the vendored subchart in `Chart.yaml`

```yaml
dependencies:
  - name: dhcp-api-token
    version: 0.1.0
    repository: ""
```

`helm template` loads a chart in `charts/` with or without this, but **`helm lint`
refuses it as undeclared**. The empty repository is what marks it already-present —
any real URL or a `file://` path would make `helm dependency build` try to resolve
it, which must not happen with no chart repo reachable. **Do not commit a
`Chart.lock`.** Verify with `helm dependency list .` → status `unpacked`, no network.

Also delete the `testRunner` block from `values.yaml` and the `TEST_RUNNER_*` env
vars from `templates/deployment.yaml` (repo 1b).

---

## Repo 3: `gitops-day2-prod` (`sigs/redbull`) — one Secret per MCE — **AIR-GAPPED ONLY**

NEW FILE:
`sigs/redbull/mces/in-cluster-defaults/dhcp-api-token/dhcp-api-token.yaml`

```yaml
projectNamespace: dhcp-scope-manager
repourl: https://8200gitlab[REDACTED]/redbull/helm-charts/dhcp-scope-manager.git
targetRevision: main
path: charts/dhcp-api-token
syncPolicy:
  automated:
    selfHeal: true
    prune: true
  syncOptions:
    - CreateNamespace=true
```

- **`repourl`, all lowercase** — that is the key `deployApp.yaml` reads
  (`.Values.repourl`). Several existing deploy configs write `repoUrl`; that spelling
  reaches the template as nil and renders an empty `repoURL`. Do not copy it.
- **`path: charts/dhcp-api-token`** deploys the subchart alone — the repo root is the
  full API deployment, and an MCE needs the Secret and nothing else. `deployApp.yaml`
  already supports `path` (`{{ .Values.path | default "." }}`), but **no existing
  chart here uses it — render once before merging.**
- **No `values.yaml` in this folder.** The token comes from the subchart's own
  values, which is the base of the merge in both render paths. That is what makes
  rotation one file for both the mgmt cluster and every MCE.
- `projectNamespace` matches the mgmt cluster's namespace on purpose, so
  `tokenSecretRef.namespace` is one literal everywhere. Here it holds only the token.
- `prune: true` is deliberate — deleting the folder must remove the Secret rather
  than strand a live credential. The flip side: an accidental deletion 401s every
  scope write on that MCE.

Honour the folder's own rules: the `mcesAppset` exclude must already be live, and
every directory here becomes an Application.

---

## Repo 4: `helm-charts/hostedclusters-setup` — **the only repo whose air-gapped copy differs from GitHub**

### a. `values.yaml` — add the third key

```diff
   tokenSecretRef:
     name: dhcp-scope-manager-token
     key: api-token
+    namespace: dhcp-scope-manager
```

The GitHub copy gets the same literal (a no-op there, since `crossplane.namespace`
already resolves to it). **In this copy it is the whole change**: without it,
`namespace` falls back to `dhcp.crNamespace` = `hcp-<clusterName>`, which is exactly
what required a copy of the Secret in every hosted cluster's namespace.

Still required even though GETs are anonymous — a Request writes.

### b. DELETE the per-hosted-cluster Secret template — **AIR-GAPPED ONLY**

The GitHub copy has never had one, so there is nothing to diff against. Find it:

```sh
grep -rln "kind: Secret" templates/
```

Delete it outright, not behind a flag — a flag is a second thing to keep in step,
which is the bug being removed. Then:

- check `_dhcp-helpers.tpl` for a `define` that template was the only consumer of;
- **check `values.yaml` for the key that fed it** (a literal token, or
  `dhcp_api.token`). That key must go too, or the token stays committed in a second
  repo and this change achieves nothing.

### c. Comments that are now false

Three blocks assert the CR's namespace and the token's cannot drift, because one
variable fed both. That is no longer true and the coupling is what was removed:

- the block above `Authorization:` in `templates/dhcp-scope-request.yaml`;
- the `metadata.namespace` comment in the same file;
- the `dhcp.crNamespace` header in `templates/_dhcp-helpers.tpl` ("Load-bearing twice
  over" — it is load-bearing once now).

The replacement wording is in the GitHub-side commit. Keep every sentence about the
three-segment regex and the unparseable-placeholder-is-literal-text failure: those
matter **more** now, because the two namespaces are separate values and so *can*
disagree.

### d. Tests

`test_derived_namespace_is_shared_by_the_token_secret` asserted the old coupling and
is replaced by `test_token_namespace_is_independent_of_the_cr_namespace`, which
asserts the CR is in `hcp-cluster-a` while the header names `dhcp-scope-manager`.
Plus a new `test_no_token_secret_is_rendered`.

Verified: `test_token_namespace_defaults_to_the_cr_namespace` keeps passing
unchanged — Helm 3 treats an explicit `~` in a *user* values file as deleting the
key, so the fallback still resolves.

The end-to-end assertion, which is the change in one line:

```sh
helm template hc . --set clusterName=<cluster> -f <cluster values> | grep -E "^  namespace:|Bearer"
#   namespace: hcp-<cluster>
#   "Bearer {{ dhcp-scope-manager-token:dhcp-scope-manager:api-token }}"
#              ^ the two namespaces DIFFER — that is the whole point
```

---

## Repo 5: `workflows` + `helm-charts/segment-lifecycle-worker` — drop the token

**Only after the new API image is live.** Not deployed air-gapped today, so this is
low risk here — port it for consistency.

- `activities/segment_lifecycle/activities.py` — `_dhcp_api_client()` drops the
  `Authorization` header; keeps `base_url` and timeout.
- `shared/settings.py` — remove `dhcp_api_token`. Keep `dhcp_api_url`.
- `.env.example` — remove `DHCP_API_TOKEN`.
- worker chart `templates/config.yaml` — delete the `dhcp-api-token` Secret template.
- worker chart `templates/activity-worker.yaml` — delete the `envFrom.secretRef`
  entry for it. **This one matters operationally:** an `envFrom` naming a Secret that
  no longer exists blocks pod start, so the template and the Secret must go together.
- worker chart `values.yaml` — remove `secrets.existingDhcpSecret` and
  `secrets.dhcpApiToken`.

`allocate_segment`'s `awaiting-dhcp-scope` phase is unchanged — it still polls and
still has its convergence deadline. Only the header goes.

---

## Repo 6: `redbull-platform` (or its air-gapped equivalent)

- `gitops/services/prod/dhcp-scope-manager/values.yaml` — `apiAuth.existingSecret` →
  `apiAuth.secretName` (same value, `dhcp-scope-manager-token`). Drop the
  `oc create secret` instructions and the "rotate the two together" note.
- `gitops/services/prod/segment-lifecycle-worker/values.yaml` — remove
  `existingDhcpSecret`.
- **DELETE `gitops/services/prod/dhcp-scope-manager-tests/`** — the second release
  existed to host the test-runner endpoint, which is gone.
- `gitops/SECRETS.md` — new row for the DHCP token.

If the air-gapped equivalent has other files setting `apiAuth.existingSecret`, grep
before renaming:

```sh
grep -rn "apiAuth" .
```

---

## Live cleanup, after everything is synced

```sh
# The worker's copy — no longer referenced by anything
oc delete secret dhcp-api-token -n redbull-workflows

# Should be exactly ONE per MCE, in dhcp-scope-manager
oc --context <mce> get secret -A -l app.kubernetes.io/name=dhcp-api-token

# Any leftovers in hcp-* namespaces — don't assume Argo pruned them
oc --context <mce> get secret -A --field-selector metadata.name=dhcp-scope-manager-token
```

**The API pod must be restarted whenever the token changes**, including the first
time this lands:

```sh
oc rollout restart deploy/dhcp-scope-manager -n dhcp-scope-manager
```

`DHCP_API_TOKEN` is env, materialised at pod start. Argo updating the Secret does
**not** restart the Deployment, and without the restart every Crossplane write 401s
indefinitely. This is the single most likely way to get this wrong.

## Verifying it worked

```sh
# Same token everywhere — compare hashes, never print it
h() { oc "$@" -o jsonpath='{.data.api-token}' | base64 -d | shasum -a 256 | cut -c1-12; }
h --context mgmt  get secret dhcp-scope-manager-token -n dhcp-scope-manager
h --context <mce> get secret dhcp-scope-manager-token -n dhcp-scope-manager
oc exec deploy/dhcp-scope-manager -n dhcp-scope-manager -- printenv DHCP_API_TOKEN \
  | shasum -a 256 | cut -c1-12

# Syncs no longer rotate it — the regression this whole change is about
oc get secret dhcp-scope-manager-token -n dhcp-scope-manager \
  -o jsonpath='{.data.api-token}' | shasum -a 256
argocd app sync dhcp-scope-manager && argocd app sync dhcp-scope-manager
# same hash, and the pod did not restart
```

**Detecting a wrong token namespace.** The CR always shows the placeholder by
design, so inspecting it proves nothing — resolve it yourself:

```sh
oc --context <mce> get requests.http.m.crossplane.io -A \
  -o jsonpath='{range .items[*]}{.spec.forProvider.headers.Authorization[0]}{"\n"}{end}' \
| sed -E 's/.*\{\{ (.*):(.*):(.*) \}\}.*/\1 \2 \3/' | sort -u \
| while read n ns k; do
    oc --context <mce> get secret "$n" -n "$ns" -o jsonpath="{.data.$k}" >/dev/null 2>&1 \
      && echo "OK   $ns/$n[$k]" \
      || echo "FAIL $ns/$n[$k] -> provider-http is sending literal text; every write 401s"
  done
```

Now that GETs are anonymous the failure is **quieter than it used to be**: OBSERVE
succeeds and only creates/updates 401, so the Request sits `Synced: True` /
`Ready: False` with the scope simply never appearing. That is why this check matters.

## Rotating, from here on

1. Edit `token:` in this environment's `charts/dhcp-api-token/values.yaml`. One
   commit, one file — it reaches the mgmt cluster and every MCE.
2. Let Argo sync.
3. `oc rollout restart deploy/dhcp-scope-manager -n dhcp-scope-manager`.

The 401 window between 2 and 3 is bounded and self-healing: all four verbs are
idempotent, a 401 leaves the Request unready with no partial write, and provider-http
retries on its own ~60s cadence.

## When Vault arrives

The refactor is confined to `charts/dhcp-api-token/templates/token.yaml`: render an
`ExternalSecret` with the same `metadata.name` and the same `data[].secretKey`, and
drop the `token` value. `apiAuth.secretName` and `dhcp_api.tokenSecretRef` do not
move, no consumer changes, and rotation stops touching git entirely.

**That migration must include a rotation.** The token is committed, and git history
is permanent.
