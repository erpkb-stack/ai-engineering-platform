# Phase 22a - Kubernetes on kind (1 replica each; ADR-034)

Status: VERIFIED on the Mac (2026-10-17), after fix1 + fix2. The sandbox cannot run kind
(registries blocked): the generator, the policies and the deploy logic are unit-tested there;
everything below marked "Mac" ran on the owner's Intel Mac.

## 0. Gate and scope
- **Gate: passed.** Phase 21 verified on the Mac and merged (PR #24, main d53f88c).
- **Owner decisions (2026-10-16):** split the roadmap row. **22a** = kind, probes, zero-downtime
  rollout demo, ENFORCED NetworkPolicies, 1 replica per service. **22b** (after 22a is verified)
  = 2+ replicas, Redis shared rate limit + LLM cache (ADR-031), HPA only where CPU really moves.
  Postgres, Kafka, Keycloak, Redis **stay in compose** (the "managed services").
- **The honest reason:** compose already runs everything. Kubernetes adds three things compose
  cannot show: rolling updates without a dropped request, more than one replica (22b), and
  network rules that are enforced. 22a does the first and the third.
- **Cut from the roadmap row (mentor mode):** HPA on CPU for the agents - they wait on model
  calls (I/O), their CPU stays low, an HPA would never scale them (22b measures first).
  StatefulSets for the infra - would double the 2.9 GiB already in compose. kustomize - see
  ADR-034 "Changed during the build".

## 0b. Measured on the Mac (2026-10-16, before the build)
| Fact | Value |
|---|---|
| Tools | kind 0.33.0 (node Kubernetes 1.37.0), kubectl 1.36.1 (skew 1: supported) |
| Empty cluster | 691 MiB (control plane), ready in 15-26 s |
| Budget | infra ~0.7 GiB in use + cluster 0.7 + apps ~1.0 (Phase 21 peaks) = ~2.4 of 7.75 GiB |
| Reachability | after `docker network connect aeoi_default <node>`: pods reach `postgres:5432`, `kafka:19092`, `host.docker.internal:5433` |
| NetworkPolicy | kindnet ENFORCES them: deny-all blocked pod->pod (`web`) AND pod->compose (`postgres`) |
- My probe scripts failed once: zsh does not split a command stored in a variable. The new code
  is Python with argument lists; the Make recipes are one command each (or `set -eu -o pipefail`).

## 1-2. Objective and reason
The same containers as Phase 21 run in kind, with startup/readiness/liveness probes, measured
memory requests, a default-deny network, and the EXISTING smokes pass against them unchanged.
Then two demos: (a) a rolling restart of api and incident-service under continuous traffic with
**0 failed requests**; (b) every forbidden call **fails** (agents -> Postgres is the headline).
KPI: none directly - an enabler for 22b and Phase 24 (CI deploys to kind).

## 3. Architecture
```
host (Mac)  127.0.0.1:8000-8006,8008,8010 --(kind extraPortMappings -> NodePort 308xx, DEV file only)--+
                                                                                                       v
  kind node `aeoi-control-plane` (a container, ALSO on docker network aeoi_default)   ns ai-engineering-platform
    api  incident-service  orchestrator  agents  rag  llm-gateway  tool-gateway  audit  [service-catalog]
    (Deployments, 1 replica, aeoi-py / aeoi-catalog loaded with `kind load`, never pulled)
        |  default-deny; allow = exactly the calls the config makes; Pod Security "restricted"  |
        v                                                                                      v
  compose: postgres:5432  kafka:19092  keycloak:8080  (same names as Phase 21)          host: Ollama :11434
```
Deploy (`make k8s-apply`, scripts/k8s_deploy.py): `docker compose config` (your `.env`, the
same profiles as `make apps-up`) + facts read from Docker and the cluster (compose subnet, host
gateway, node addresses) -> `scripts/k8s_manifests.py` -> `.k8s/manifests/` -> server dry-run
(schema + Pod Security) -> Secrets/ConfigMaps from the files -> apply -> wait for every rollout.

| Guarantee | Enforced by | Test |
|---|---|---|
| Same config as compose | generated from `compose config`; unknown compose keys are errors | unit: command, env, port, image == compose; 20 "unexpected compose" cases; real `docker compose config` == the test fixture |
| Non-root, read-only, no privilege | namespace Pod Security `restricted` (enforce) + securityContext 10001, read-only root, drop ALL, seccomp, no SA token, no service links | unit: every pod + init container; Mac: apply's server dry-run |
| Ready = can serve; live = process only | readiness /health/ready, liveness /health/live, startupProbe (JVM: 180 s) | unit; Mac: rollout demo |
| Only own secrets | one Secret per service = its compose mounts, 0440 + fsGroup 10001 | unit: files == compose, mount, mode |
| Default deny | `default-deny` + `allow-dns` (kube-dns :53 only) | unit; Mac: netpol-test |
| Allowed = needed | per-app policy from the URL graph; infra/host/internet rules by stated rule | unit: both ways vs the SETTINGS classes; Mac: the full matrix |
| No registry | `imagePullPolicy: Never`, `kind load` | unit |
| Zero-downtime | maxUnavailable 0 / maxSurge 1, readiness, preStop sleep 5 s, grace 30 s | Mac: 0 failed requests |
| compose depends_on | initContainer `wait_for.py` (orchestrator waits for agents, api, incident-service READY) | unit |
| Never your other cluster | every kubectl call is `--context kind-aeoi` | unit (AST check of the script) |

[P] kind, NodePorts on loopback, Secrets from files. [Prod] EKS, only api behind an ALB ingress,
External Secrets Operator, managed RDS/MSK, migrations as a Job. [Ent] GitOps + admission policies.

## 4. Files
```
infrastructure/kubernetes/kind/cluster.yaml   1 node, the 9 app ports on 127.0.0.1 -> NodePort 308xx
scripts/k8s_manifests.py                      compose config + facts -> manifests (pure)
scripts/k8s_deploy.py                         up / apply / render / status / netpol-test / rollout-demo / down
infrastructure/docker/wait_for.py             initContainer (in the aeoi-py image: REBUILD with make images)
Makefile                                      kind-up kind-down k8s-manifests k8s-apply k8s-status
                                              k8s-netpol-test k8s-rollout-demo (help now lists targets with digits)
tests/unit/test_k8s_manifests.py              150 tests     tests/unit/test_k8s_deploy.py  33 tests
.gitignore                                    .k8s/
docs: ADR-034, this file, roadmap, architecture.md §22, CLAUDE.md, infrastructure/CLAUDE.md, interview notes
```
Mutation check (sandbox): 46/46 deliberate breaks of the generator caught, 18/18 of the deploy
script (judge, facts, context pinning, preflight, Pod Security stop, pacing).

## 8. Verify (on the Mac, 2026-10-16/17) - all ticked from real output
- [x] `make check` 1142 passed (952 before this phase)
- [x] `make images`, `make up` (your .env: Kafka on, Java catalog on, Keycloak issuer on)
- [x] `make kind-up` -> cluster created, node joined aeoi_default (one failed create, see 9b)
- [x] `make k8s-apply` -> 9/9 Deployments available (8 Python + the catalog), restarts 0
- [x] The smokes UNCHANGED against the cluster: smoke 12/12, multi-smoke 26/26, tools-smoke 16/16,
      approval-smoke 23/23, risk-smoke 19/19, catalog-smoke 6/6 (parity 0 mismatches), kc-smoke 21/21
- [x] `make k8s-netpol-test`: with Keycloak up 99 passed / 0 failed; after fix2, 105 passed /
      16 skipped / 0 failed (8 = a pod calling its own Service, 8 = keycloak:8080 while it was
      stopped; with it running all 8 Python apps reached it). `agents -> postgres:5432` blocked
      while api/rag/... opened it; the probe pod reached no app port; only llm-gateway reached
      api.anthropic.com:443 and Ollama (the others: `unreachable`)
- [x] `make k8s-rollout-demo`: api and incident-service each replaced (43 s / 35 s), 0 of 1,323
      requests failed (105 product GETs, 609 + 609 /health/live straight to each pod, ~8 req/s each)
- [x] `make k8s-status` -> restarts 0 after the demo
- [x] `make kind-down`, then `make apps-up` 12/12 healthy (ports free again), `make apps-down`

## 8b. Measured on the Mac
| What | Value |
|---|---|
| Node memory, 9 apps running | 1.72 GiB after apply, 1.84 GiB after the demo (empty cluster: 0.69 GiB) |
| Node CPU, 9 apps, no traffic | 10 samples 89-141 %, median ~104 % (about 1 of 12 cores) |
| How much of that CPU is Kubernetes itself vs our 9 apps' probes | NOT measured (the empty-cluster samples were not taken): open item |
| Rollout time with 1 replica | api 43 s, incident-service 35 s (start + readiness + old pod drained) |
| Demo resolution | ~8 req/s per pod: gaps under ~0.15 s, heavy load and POSTs are NOT tested |

## 9. Fixes from the Mac
- **fix1** `make k8s-apply` stopped: "redis: neither an app nor known infra". Your .env turns
  Kafka on, the kafka profile brings redis along, and redis was missing from the generator's
  infra list. The tests only built the apps and apps-catalog profiles. Now: redis is infra (no
  rule: no app calls it until 22b), and a test builds all four profile sets (it failed before).
- **fix2** `make k8s-netpol-test` was 91 passed / 0 failed / 30 skipped. 14 skips were real
  denials mislabelled "target down": forbidden pods got `EHOSTUNREACH` for api.anthropic.com:443
  and host.docker.internal:11434 while llm-gateway OPENED both. The probe now names that answer
  `unreachable` and counts it as closed; any other error against a target that is up is shown as
  "look", never as a pass. 8 skips are a pod calling its own Service (not a call any service
  makes); 8 are keycloak:8080 (your issuer switch is on, Keycloak was not running).

## 9b. Seen on the Mac, cause not proven
- One `kind create cluster` failed: kubeadm's first API call timed out for ~1 min ("context
  deadline exceeded"); kind removed the node. The same config worked before and right after.
  At that moment Docker used ~1.2 GiB and little CPU (Keycloak 0.5 %), so neither memory nor CPU
  explains it; disk was not checked in full. After `make keycloak-down` the retry was ready in
  14 s. Recorded as a flake, not fixed; if it repeats: `docker system df` (5.3 GB images and
  1.9 GB build cache were reclaimable) and the Docker Desktop disk limit.

## 10. Open items (each its own later change)
- Repo-wide make 3.81 `.SHELLFLAGS` (39/116 targets), the wall-clock tool-gateway timing test,
  the structlog context leak, the port-8001 precheck miss, the Kafka probe trade-off (Phase 21).
- 22b: replicas + PDB, Redis (ADR-031), HPA after measuring CPU.
- Split the ~1 core idle CPU: empty-cluster samples vs with the apps (probe periods are 5-10 s).

## 11. Known risks (first cluster run)
| Risk | Why | Plan |
|---|---|---|
| Kubelet probes blocked by the policies | the probe source address is a kindnet detail | ingress from EVERY IPv4 address of the node + the pod-range gateway, read at deploy time; if pods stay unready, `kubectl --context kind-aeoi -n ai-engineering-platform describe pod` shows probe timeouts |
| The smokes blocked by the policies | the Mac's traffic arrives from the `kind` network gateway (independent review: kindnet checks before the node rewrites the source) | `dev-host-ingress` admits that gateway on the app ports (dev file only) |
| Pod DNS for compose names | only after `docker network connect`, redone on every cluster create | `kind-up` does it; `apply` refuses without it |
| The subnet of aeoi_default changes | Docker picks it | read at deploy time, never written in a file |
| Rollout drops requests | endpoint removal races SIGTERM | preStop 5 s + readiness; the demo counts every response |
| Docker Desktop restart | kind nodes do not always come back cleanly | `make kind-down && make kind-up && make k8s-apply` |
| `kind load` with Docker Desktop's containerd image store | "content digest not found" has been reported for images with provenance attestations | if it happens: build with `--provenance=false` (a later fix, not guessed now) |
| api readiness depends on incident-service readiness | one blip makes the entry point unready (cascade) | existing design (Phase 4); revisit in 22b with 2 replicas |
