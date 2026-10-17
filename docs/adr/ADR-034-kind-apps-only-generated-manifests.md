# ADR-034: Apps on kind, infra stays in compose; manifests generated from docker-compose.yml

Status: Proposed (verified on the Mac 2026-10-17: 9/9 apps, smokes unchanged, netpol 0 failed, rollout 0 failed)
Date: 2026-10-16
Refines: ADR-009 (Kubernetes), ADR-033 (images), architecture.md §22; splits roadmap Phase 22

## Context
Phase 21 runs every service as a container, verified, with measured limits. ADR-009 chose kind
locally. architecture.md §22 puts Postgres/Kafka/Redis in the cluster as StatefulSets and puts
HPA on CPU for the agents. Measured on the owner's Mac (2026-10-16): an empty kind cluster uses
691 MiB; pods reach the compose infra by its compose names once the node joins `aeoi_default`;
kindnet enforces NetworkPolicies (pod->pod and pod->compose both blocked by deny-all).

## Decision
- **Split Phase 22.** 22a: kind, manifests, probes, rollout demo, enforced NetworkPolicies, ONE
  replica each. 22b: replicas, Redis (ADR-031), HPA where CPU really moves.
- **Only the apps run in kind.** Postgres, Kafka, Keycloak, Redis stay in compose, as the stand-in
  for managed services (RDS, MSK). Migrations stay host commands.
- **Manifests are generated from `docker compose config`** (`scripts/k8s_manifests.py`) at DEPLOY
  time into `.k8s/manifests/` (gitignored). Same service names, command, environment and per-file
  secrets as Phase 21; compose has already read `.env`, so the switches mean the same in both.
  The generator adds only: probes, resources (memory request = measured peak, limits = compose's),
  a hardened pod, an initContainer per compose `depends_on` on an app, and NetworkPolicies.
  A compose key it does not know is an ERROR, never dropped.
- **Pod Security "restricted"** is enforced on the namespace: the API server refuses a root or
  privileged pod, not only our tests. `k8s-apply` treats a dry-run PodSecurity warning as an error
  (for a Deployment it is only a warning, and the pods would then never start).
- **Default deny.** Each pod may call exactly what its configuration calls (the URL graph; the
  test takes its truth from the Settings classes, like the Phase 21 contract). Postgres only for
  services holding a DB login (agents: none), Kafka only for the services whose code uses it,
  port 443 to the internet (private ranges and metadata excepted) only for llm-gateway, the host
  (Ollama) only for llm-gateway. Compose infra and node addresses are ipBlocks read at deploy time.
- **Secrets** are created by kubectl from the same files (server-side apply: no last-applied copy),
  mounted at `/app/secrets` with mode 0440 + fsGroup 10001: the Phase 21 644 step is not needed.
- **No registry:** `kind load`, `imagePullPolicy: Never`. Every kubectl call pins
  `--context kind-aeoi`.

## Changed during the build (vs the reviewed draft)
- **Not committed, no kustomize.** The draft said "committed + drift test" and a kustomize overlay.
  Committed manifests would have needed a placeholder layer (the subnet, node addresses and `.env`
  switches are only known at deploy time) that compose interpolation already does, plus a drift
  test to keep the copy equal to its source. Generating at deploy time has nothing to drift.
  The dev-only part (NodePort services) is its own file, `40-dev-host-ports.yaml`. kustomize is
  the right tool once several environments need patches over one base ([Prod], Phase 24).
- `make k8s-manifests` writes the rendered files for review without applying them.

## Alternatives
| Option | Why not (here) |
|---|---|
| StatefulSets for the infra in kind | ~2x infra memory on 7.75 GiB; production would use managed services anyway |
| Hand-written manifests (+ kustomize) | 9 services x 5 objects that drift from compose; the drift is the bug |
| Committed generated manifests + drift test | placeholders for deploy-time facts; two copies of one truth |
| kompose (compose -> k8s converter) | no probes, policies or hardening; its output would be rewritten anyway |
| Calico for policies | not needed: kindnet enforces them (measured) |
| Everything in one Phase 22 | cluster bugs and Redis bugs in one Verify |

## Tradeoffs
- Pods depend on Docker DNS for the infra names (works on Docker Desktop; recorded as [P] only).
- The infra egress rules allow the whole `aeoi_default` subnet on the infra port (container IPs
  change on restart); the port narrows it.
- Ingress from the node addresses on each app's own port (kubelet probes, NodePort SNAT).
- The dev file opens all 9 app ports on loopback for the unchanged smokes; production exposes api only.
- The cluster holds 127.0.0.1:8000-8010 while it exists: compose apps / `make dev` need `make kind-down`.
- A rotated secret needs `kubectl rollout restart` (settings are read at start). No prune: an
  object from an earlier switch state stays until `make kind-down`.
- llm-gateway may reach ANY public host on 443 (an exfiltration path if it were compromised);
  [Prod]: an egress proxy or FQDN policy limited to the provider hosts.
- Kafka egress for incident-service and audit is open even with the `log` transport (static rule).
- Host traffic (the Mac's 127.0.0.1:80xx) arrives from the `kind` network gateway: a separate
  `dev-host-ingress` policy in the dev file admits it; kubelet probes are admitted from every
  IPv4 address of the node (which one kindnet uses is an implementation detail).
- The rollout demo: the product GET (1.7 req/s, under the rate limit) is retried by the api proxy
  on a reset, so it alone could hide a drop; two 20 req/s loads hit each pod's /health/live through
  its NodePort with no retry. It proves the drain order, not behaviour under load or for POSTs.
- A rebuilt `:dev` image: the pod template carries the image ID, so `k8s-apply` rolls the pods.
- No ingress controller in 22a.

## Prototype vs Production vs Enterprise-scale
[P] kind + compose infra + NodePorts on loopback, Secrets from files. [Prod] EKS, managed data
services, ALB to api only, External Secrets, migration Job, kustomize overlays per environment.
[Ent] GitOps (rendered manifests in git) + admission policies (Kyverno/Gatekeeper) for the same rules.

## When we would revisit
A phase needs the infra inside the cluster (e.g. a chaos test that kills Postgres pods), compose is
retired, or a second environment needs the same manifests (then: commit them + kustomize).
