# Phase 22a - Kubernetes on kind: interview notes (ADR-034)

Measured facts only (Mac, 2026-10-16/17): empty cluster 691 MiB, 9 apps 1.84 GiB, ~1 core idle;
the netpol matrix 105 passed / 0 failed; a rolling restart of api and incident-service under
traffic: 0 of 1,323 requests failed (about 8 req/s per pod - gaps under ~0.15 s are not tested).

## Questions
1. Why are your manifests generated, and not written by hand or with kustomize?
2. How do you know your NetworkPolicies allow exactly what is needed?
3. Why is the database not in the cluster?
4. How do you get zero downtime with ONE replica?
5. Readiness vs liveness - what goes in each?
6. How did you keep the deploy script from touching a real cluster?

## 30-second answers
1. Compose already holds the tested truth: command, env, secret files per service (Phase 21,
   checked against the Settings classes). Hand-written manifests are a second copy that drifts.
   The generator reads `docker compose config` and adds only what is Kubernetes-specific. Any
   compose key it does not understand is an error - it never drops a setting silently.
2. Two sides. The rules come from the configuration's own URLs; the unit test takes its truth
   from the Settings classes, not from the generator, and checks both directions (nothing
   missing, nothing extra). On the cluster, every pod tries every target and the result must
   equal that graph. A forbidden target only "passes" if an allowed pod proved it is up -
   otherwise the check is reported as skipped, not as a pass.
3. It is the stand-in for a managed database. StatefulSets would double the infra memory on a
   7.75 GiB laptop and teach nothing production uses. Pods reach it by its compose name; the
   egress rule is the compose subnet plus the port, read at deploy time.
4. `maxSurge 1, maxUnavailable 0`: the new pod must be READY before the old one is stopped. A
   preStop sleep keeps the old pod serving while its endpoint is removed, then SIGTERM, then
   uvicorn drains. The demo counts every response during two restarts.
5. Liveness = the process works, no dependency checks: a DB outage must not restart every pod.
   Readiness = can serve now, with dependency checks. A startup probe covers the JVM's slow start
   so liveness does not kill it.
6. Every kubectl call pins `--context kind-aeoi`, and a unit test parses the script and fails if
   anything calls kubectl another way. Commands are argument lists, never a shell string.

## 2-minute answers
**Q2 (policies).** Default deny for the whole namespace, DNS to kube-dns only, then one policy
per app: ingress from its callers on its port, egress to its callees on their ports. Pods are not
the only targets: Postgres only for services that hold a DB login (the agents have none - the
demo is agents -> Postgres failing), Kafka only for the two services whose code uses it, port 443
to the internet with private ranges and the metadata address excepted, for the LLM gateway only.
A probe pod that may send anywhere inside the namespace tests the INGRESS side alone: no app
admits it, so every port must stay closed to it.

**Q1 (honest trade-off).** I first planned committed manifests plus a drift test. During the build
the deploy-time facts (compose subnet, node addresses, `.env` switches) would have needed a
placeholder layer that duplicates compose interpolation. Generating at deploy time has nothing to
drift. What I gave up: the rendered YAML is not in git. With a second environment or GitOps I
would commit the rendered output and add kustomize.

**Pod Security.** The namespace enforces "restricted", so the API server - not only my tests -
refuses a root pod. One trap: for a Deployment it is only a warning; the pods then never start.
The deploy treats a dry-run PodSecurity warning as an error.

**What I would say I did NOT prove.** The rollout demo is low traffic and GET only: it proves the
drain order (ready before stop, preStop before SIGTERM), not behaviour under load or for POSTs,
which the api does not retry. The idle CPU (~1 core) is not yet split between Kubernetes and our
health probes. One cluster create failed with an API timeout I could not explain - I recorded it
instead of adding a retry that would hide it.

**Two bugs the Mac found that my tests missed.** (1) The kafka profile brings redis along; my
generator called it unknown, because the tests built only two of the four profile sets. (2) My
netpol judge labelled 14 real denials "target down": the policy answered "host unreachable",
not a timeout. Both fixes came with a test that failed before.
