# Phase 21 - Docker: interview notes (ADR-033)

Numbers here are measured (Mac or sandbox, 2026-10-14). Memory limits are added after the Mac run.

## Questions
1. Why ONE image for eight services instead of one per service?
2. How do you know no container still calls `localhost`?
3. How do you keep secrets out of images and give each container only what it needs?
4. A container healthcheck vs a Kubernetes probe - which did you build and why readiness?
5. How did you choose memory limits?
6. Your Kafka broker used 2 CPU cores while idle. How did you find the cause?

## 30-second answers
1. All eight share one lock file and nearly one dependency set: the runtime venv is 192 MB.
   Eight Dockerfiles would build eight near-identical images and test nothing new. Each container
   picks its command; Kubernetes still rolls each Deployment on its own. I revisit when a service
   needs a heavy dependency the others do not.
2. A unit test reads each service's Settings class, finds every outbound URL setting, and checks
   that compose sets it to a compose service name with that service's port. It also scans the code
   for addresses read with `os.environ` - the database host was one, invisible to a settings-based
   test until review. 24 deliberate breaks of the compose file were all caught.
3. Build contexts are allow-lists, so `secrets/` never reaches the builder. At run time each
   container mounts exactly the files its settings read - tested both ways - read-only. I say
   honestly that compose secrets outside swarm are bind mounts, not a vault.
4. One HEALTHCHECK in the image, on `/health/ready`, port from an env var, so one image works for
   every service. Readiness, because "process up" is not "can serve": the orchestrator resumes
   investigations at start and must wait until api and incident-service are READY.
5. Not by guessing. A provisional 512 MB ceiling fits the 4.8 GiB left after the infra limits.
   Then I raise it, run every smoke, sample `docker stats` peaks, and set limit = peak + headroom.
6. I sampled 20 times in three states - our services off, only audit, only incident-service. The
   median was about 2 % in every state, with spikes to the 1.5-core cap in every state. So it was not
   our code: the healthcheck started a second JVM every 10 s, and that JVM even inherited the
   broker's 512 MB heap. My first single sample had pointed at our code - one sample proves nothing.

## 2-minute answers
**Q2 (addresses).** The classic container bug is a default of `localhost` that is fine on a laptop
and wrong in a container, where `localhost` is the container itself. Checking the compose file
against itself proves nothing, so the test takes the truth from the code: every `*_url` field of
every service's Settings class must be set, to an existing compose service, on that service's
container port. Then the gaps: settings read outside Settings (the DB host via `os.environ`), the
Keycloak keys (fetched from `keycloak:8080` while the token `iss` stays `localhost:8090`, so the
same token verifies everywhere), the Kafka internal listener, and the gateway's egress allow-list,
which must equal the hosts of its HTTP tools in BOTH directions - a missing host breaks a tool, an
extra one widens egress. An independent review still found three blind spots (os.environ addresses,
`env_file`/`privileged` not forbidden, ignore-rule order). I added each as a test and proved it
with a deliberate break.

**Q3 (secrets).** Three layers. Build: per-Dockerfile allow-list ignore files - everything out,
then only code, then `secrets`, `.env*` and `data` out again; a test checks the ORDER, because a
later rule wins. Run: one compose secret per file, and a test that each container's mounts equal
exactly what its settings read - the agents container cannot read the delegation signing key, the
api cannot read the deploy writer's password. Values: no secret in any env var (visible in
`docker inspect`); the Java service's entrypoint reads its password file into the process env only.
One honest limit: bind mounts keep the host file mode, so the files the containers mount are 644
inside a 700 folder - an explicit owner step, checked before start.

**Q6 (Kafka CPU).** See the 30-second answer; the lesson I tell is the method. I first blamed our
consumer from one sample, then distrusted it: `docker stats` samples about two seconds, and a JVM
that starts every ten seconds is hit or missed by luck. Twenty samples per state gave medians and
maxima, and `docker top` showed two java processes. The reviewer then found the second cause I had
missed: the probe inherited `KAFKA_HEAP_OPTS`. Fix: the probe gets its own small heap and a
fast-start JIT, runs every 2 s only while the broker starts, then every 60 s.
