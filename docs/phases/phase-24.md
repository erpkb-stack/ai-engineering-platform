# Phase 24 - CI catch-up + images (ADR-036)

Status: **24a merged (PR #31, main 6c5db2d). 24b BUILT in the sandbox (2026-10-21), Verify =
its PR's checks.**

## 0. Gate and scope
- **Gate: passed.** 23c verified on the Mac and merged (PR #28).
- **Owner decision (2026-10-21): "CI catch-up + images".** CI green and covering Phases 9-23,
  the Java `mvn verify`, the console check, both images built and Trivy-scanned on every PR.
  **No GHCR push, no deploy, no environments.**

## 0b. Found during the need check (real gaps)
1. **CI was red since PR #8 (2026-10-08).** 22 PRs merged on red. Fixed first, in two PRs:
   - PR #29: the 0026 downgrade did `DROP ROLE obs_reader`. Roles are server-wide, so the dev
     DB's grants made it fail and the half-downgraded test DB caused 29 failures. Now the role
     stays (rule of 0024), and a test fails on any downgrade that drops a role.
   - PR #30: the agent smoke never started the orchestrator (needed since Phase 9). With the
     FAKE model the postmortem step is PARTIAL; the smoke accepts PARTIAL only when the
     postmortem narrative is the ONLY error.
2. **The Java tests, the console checks and the images never ran in CI.** Only on the Mac.
3. **Trivy was compromised in March 2026** (see ADR-036). The planned `trivy-action` is unsafe
   by tag.

## 1. What changed
- `.github/workflows/ci.yml`: 3 new jobs, each runs the Mac's make targets:
  - `catalog`: `.env` + random postgres password, setup-java 21 (Maven cache),
    `make up PROFILE=infra`, `make catalog-build`, logs on failure, `make clean CONFIRM=1`.
  - `frontend`: setup-node 22 (npm cache), `make ui-install`, `make ui-check`.
  - `images`: `make images IMAGE_TAG=ci`, Trivy v0.69.3 binary checked by SHA-256, scan of
    `aeoi-py:ci` and `aeoi-catalog:ci` (HIGH + CRITICAL, report only), counts in the summary.
- `tests/unit/test_ci_workflow.py`: the jobs exist with timeouts, permissions read-only, Trivy
  pinned + hash checked before unpacking, no Trivy action/image, no action on a branch.
- Rule: merge only when `gh pr checks` is all green.

## 2. Tested in the sandbox (and what could NOT be)
- `actionlint` clean; the new unit test 6/6, and it FAILS when the hash check is removed or a
  Trivy action is added (both tried).
- Trivy v0.69.3: the archive hash matches the release checksums file AND its Sigstore bundle
  (certificate: `aquasecurity/trivy` release workflow, tag v0.69.3, 2026-03-03). The scan flags
  parse (the run reached the DB download). The summary `jq` was run on a sample report.
- Console job: replayed (Node 22, `npm ci`, 43/43 tests, API types match).
- **NOT replayed here:** `mvn verify` (Maven Central is blocked in the sandbox), `make images`
  and the real scan (Docker Hub and the Trivy DB are blocked). The PR's checks are the test.

## 3. Verify (on GitHub, by the owner)
- [ ] PR opened; `gh pr checks` shows all 6 jobs green.
- [ ] `catalog`: the log shows the Java tests ran (count) - not skipped.
- [ ] `images`: both images built; the job summary has the CRITICAL/HIGH/fixable table.
- [ ] Owner reviews the baseline numbers; decides the gate (e.g. fail on fixable CRITICAL).
- [ ] Merged only after green; `main`'s run after the merge is green too.

## 3b. First run on GitHub (PR #31, run 39542641451, 2026-10-21)
- All 6 checks green; `main` green again after PR #30 (first time since 2026-10-08).
- java: JDK Temurin 21.0.12; `mvn -q` printed NO test count, so green did not prove the tests
  ran. fix1: a step counts the surefire reports and fails on 0 tests or any skipped test.
- images: both built (aeoi-py:ci 327 MB, aeoi-catalog:ci 345 MB as reported by the runner);
  `trivy.tgz: OK`, `Version: 0.69.3`.
- Scan baseline (HIGH / CRITICAL), per Trivy target:
  | image | target | HIGH | CRITICAL |
  |---|---|---|---|
  | aeoi-py:ci | debian 12.15 (OS packages) | 61 | 2 |
  | aeoi-py:ci | Python (python-pkg) | 0 | 1 |
  | aeoi-catalog:ci | Java (jar) - the Ubuntu layer had none | 31 | 10 |
- The CRITICALs (Trivy merges table cells, so 4 catalog rows stand for 10):
  - debian: sqlite CVE-2025-7458 (no fix), zlib CVE-2023-45853 (`will_not_fix`);
  - python: **certifi 2026.7.22**, GHSA-6q48-6c7w-9gw2 (bundled compromised emSign roots), fixed
    2026.10.19 - every Python service trusts that bundle;
  - jar: tomcat-embed-core 10.1.39 (fix 10.1.55), spring-security-core 6.4.4 (fix 6.4.6),
    spring-security-web (fix 6.5.9 / 7.0.4), spring-webmvc (fix listed ONLY in 7.0.9).
- Cause of the jar findings: Spring Boot 3.4.4 (March 2025), never upgraded.
- After the merge, `main`'s run was green (run 39659172205). Two annotations: `ubuntu-latest`
  moves to Ubuntu 26 from 2026-10-19; gitleaks-action v2 runs on deprecated Node 20.

## 3c. 24b - fix first, then gate (owner: "go 24b", 2026-10-21)
Checked facts (GitHub API and raw files, from the Mac's VM; Maven Central is blocked in both):
- Spring Boot releases: newest 3.x = **3.5.16 (2026-06-25)**; 4.0.8 on 2026-08-21; no 3.5.x since.
- Boot 3.5.16 manages Tomcat **10.1.55**, Spring Security **6.5.11**, Spring Framework
  **6.2.19** (3.4.4: 10.1.39 / 6.4.4 / 6.2.5).
- gitleaks-action v3.0.0 (2026-05-30): Node 24, "no changes to inputs, outputs, or behavior".

Changes:
1. `uv lock --upgrade-package certifi`: 2026.7.22 -> 2026.10.19 (the only lock change).
2. Spring Boot parent 3.4.4 -> 3.5.16 (fixes tomcat + both spring-security findings by the
   versions above). Not Boot 4: a major upgrade is its own decision.
3. The gate: `scripts/scan_gate.py` (stdlib only) fails on a fixable CRITICAL with no live
   exception, on an expired exception, on one longer than 120 days, and on a stale one.
   Unfixed CRITICALs and HIGHs are reported, never gating (nobody can upgrade them away).
4. `infrastructure/security/scan-exceptions.toml`: ONE entry, spring-webmvc CVE-2026-47884
   (fix only in Framework 7 = Boot 4), expires 2027-01-31, with the reason.
5. Every action pinned by full commit SHA (tag in a comment), gitleaks-action v3, runners
   `ubuntu-24.04` (CI and claude.yml). Guard tests: SHA pins, no `-latest`, the gate step.
Tested here: `scan_gate` 8 tests (pass, fail, expired, too long, stale, other image not
covered, no reason refused, committed file valid); the CI guard tests FAIL on an unpinned
action and on `ubuntu-latest` (both tried); actionlint clean.
NOT testable here: the Boot 3.5 build (Maven blocked) - the Mac's `make catalog-build` and CI.
Expected on the first run, honestly unknown: the other 6 catalog CRITICALs (never seen). If one
has a fix the gate fails and names it - that is the gate working, not a bug.

## 3d. Verify 24b
- [ ] Mac: `make catalog-build` (23 tests) and `make catalog-smoke` on Boot 3.5.16.
- [ ] PR: all checks green; `java tests: 23`, the gate step `scan gate: passed`.
- [ ] Summary table: aeoi-py fixable CRITICAL 0 (certifi gone); aeoi-catalog fixable 1, excepted 1.
- [ ] `main` green after the merge.

## 4. Open items (not this phase)
- Spring Boot 4 (removes the webmvc exception before 2027-01-31).
- Base images by digest ([Prod], ADR-036 "Not done").
- Push / deploy / environments: deferred by owner decision.
