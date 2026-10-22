# ADR-036: CI checks everything on every PR; images built and scanned, not pushed or deployed

Status: Proposed (24a merged PR #31; 24b adds the gate)
Date: 2026-10-21
Refines: architecture.md §23 (CI/CD), C9; ADR-033 (images)

## Context
- CI was **red from 2026-10-08 (PR #8, Phase 9) to 2026-10-21** and 22 PRs were merged on red.
  Two causes, both found only in Phase 24: the 0026 downgrade dropped a server-wide role (29
  integration failures), and the agent smoke never started the orchestrator Phase 9 added.
  Nothing enforced "green before merge".
- CI did not run the Java tests, the console checks, or build the images. Those ran only on
  the Mac, by hand.
- Trivy (the planned scanner) was compromised in March 2026: release v0.69.4, 75-76 of the 77
  `trivy-action` tags, `setup-trivy` tags, Docker Hub images 0.69.5 and 0.69.6. Aqua's advice:
  pin by full commit SHA.
- Owner decision (2026-10-21): "CI catch-up + images". No GHCR push, no deploy, no environments.

## Decision
- **Every PR runs 6 jobs:** quality, gitleaks, infra-smoke (as before), `catalog`
  (`make up PROFILE=infra` + `make catalog-build` = `mvn verify` against the compose Postgres),
  `frontend` (`make ui-install` + `make ui-check`), `images` (`make images` + a Trivy scan).
  CI runs the SAME make targets as the Mac, so "works on the Mac" and "green in CI" test the
  same thing.
- **Trivy = the v0.69.3 release binary, pinned by SHA-256 in the workflow**, checked before it
  is unpacked. No Trivy action, no Trivy image. The hash
  (`1816b632…88b75`, Linux-64bit tar.gz) was checked against the release's Sigstore bundle:
  signed 2026-03-03 by `aquasecurity/trivy`'s own release workflow at tag v0.69.3 (before the
  compromise), Rekor log index 1017902085. A version change = a new hash checked the same way.
- **Fix first, then gate (24b).** The first run (report only) found 13 CRITICALs; the
  fixable ones came from certifi 2026.7.22 and Spring Boot 3.4.4. 24b upgrades both (certifi
  2026.10.19, Boot 3.5.16 = the newest 3.x), then `scripts/scan_gate.py` fails CI on a CRITICAL
  that has a fix and no live exception. Unfixed findings and HIGHs are reported only.
  A gate set before the baseline would have been red on every PR, then loosened until it
  meant nothing.
- **Exceptions are dated promises**: `infrastructure/security/scan-exceptions.toml`, each with
  image + package + id + reason + expiry (max 120 days). Expired or stale entries FAIL the gate.
  Entries: two spring-webmvc CVEs, fixed only in Framework 7 (Boot 4), until 2027-01-31.
- **A fix inside our line beats an exception**: a PATCH override of a Boot-managed version
  (`tomcat.version` 10.1.58, the gate's first finding) with a comment saying when to remove it.
  Minor/major overrides of managed versions are not allowed: that is an upgrade decision.
- **Pinned inputs (24b):** every action by full commit SHA (resolved from its tag via the
  GitHub API, tag in a comment), runners `ubuntu-24.04` (not `-latest`, which moves to Ubuntu 26
  without a commit of ours), gitleaks-action v3 (Node 24).
- **Rule: merge only when every check is green** (`gh pr checks`). A unit test
  (`tests/unit/test_ci_workflow.py`) guards the jobs, the read-only permissions, the Trivy pin,
  "no Trivy action/image", SHA-pinned actions, pinned runners and the gate step.

## Not done (and why)
- **No push to GHCR, no kind-in-runner deploy, no dev/staging/prod environments**: owner
  decision. Nothing consumes a pushed image yet (the Mac builds its own; kind loads local
  images). [Prod] push by digest, sign (cosign), deploy by digest with environment approvals.
- **No automated SHA updates** (Dependabot): pins are updated by hand, in a PR. [Prod]
  Dependabot or Renovate for actions, keeping the SHA + tag comment.
- **Base images are pinned by tag, not digest** (`.claude/rules/infrastructure.md` asks for
  digests in CI). A digest needs a way to be updated, or it freezes old CVEs in place; it comes
  with Dependabot/Renovate, not by hand.
- **No arm64 build** (the rules ask for an amd64 + arm64 matrix): it doubles the image job's
  time for a platform nobody runs here.
- **No path filters**: every job runs on every PR. Simpler; the cost is CI minutes, not time
  on the Mac.

## Tradeoffs
- Two more jobs start Docker (catalog, images): more CI minutes per PR, in parallel.
- The gate blocks a PR for a CVE published overnight in code the PR did not touch. Accepted:
  the fix is an upgrade or a dated exception, both visible in review.
- No Boot 3.5.x release since 3.5.16 (2026-06-25). If that line has ended, new Spring CVEs
  will need exceptions until Boot 4 - the 120-day limit forces that decision.
- A pinned scanner gets no fixes by itself; updating it is a manual, reviewed change.

## Prototype / Production / Enterprise-scale
- Prototype (this): checks on every PR, images built and scanned, nothing published.
- Production: push signed images by digest, SBOM, gated scan, deploy on main with approvals.
- Enterprise: org-wide required checks, action allow-list, provenance (SLSA), private mirrors.
