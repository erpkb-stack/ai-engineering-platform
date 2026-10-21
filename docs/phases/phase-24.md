# Phase 24 - CI catch-up + images (ADR-036)

Status: **BUILT in the sandbox (2026-10-21), Verify = the PR's checks on GitHub.**

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

## 4. Open items (not this phase)
- Gate the scan after the baseline review (a small follow-up PR).
- SHA-pin all actions and base images by digest ([Prod], ADR-036 "Not done").
- Push / deploy / environments: deferred by owner decision.
