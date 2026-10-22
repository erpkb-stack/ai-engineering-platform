# Phase 24 - CI catch-up + images: interview notes (ADR-036)

Measured facts only (GitHub Actions, 2026-10-21..22, PRs #29-#32): CI had been red from
2026-10-08 (PR #8) to 2026-10-21 and 22 PRs were merged on red. Now every PR runs 6 jobs:
lint/types/unit (1211 tests), gitleaks, compose infra + integration (269 passed), Java
`mvn verify` (23 tests, counted), console checks, and both images built + scanned with a gate.
Scan, first run vs now: catalog CRITICAL 10 -> 2, HIGH 31 -> 6; Python fixable CRITICAL 1 -> 0.
The 2 left (spring-webmvc) are fixed only in Spring Framework 7 and carry dated exceptions.

## Questions
1. CI was red for two weeks. How did that happen, and what stops it now?
2. Why not just use the Trivy GitHub Action?
3. How do you introduce a security gate without blocking every PR?
4. How do you keep scan exceptions from becoming permanent?
5. What did you get wrong in this phase?
6. What would you change for production?

## 30-second answers
1. Two real bugs (a downgrade dropped a server-wide Postgres role; a smoke never started the
   orchestrator a later phase added) and no rule that a merge needs green checks. Now: the rule
   "merge only when `gh pr checks` is all green", CI runs the same make targets as the laptop,
   and a unit test guards the workflow itself (jobs, permissions, pins, the gate step).
2. Trivy was compromised in March 2026: a malicious release and 75-76 of 77 action tags moved.
   A tag is a pointer anyone with push rights can move. I use the v0.69.3 release binary,
   pinned by SHA-256 and checked before unpacking; the hash was verified against the release's
   Sigstore bundle (signed by Trivy's own release workflow before the attack). Every other
   action is pinned by full commit SHA too.
3. Report first, then fix, then gate. The report-only run showed 13 CRITICALs; a gate on day
   one would have been red everywhere and then loosened until it meant nothing. I fixed what
   had a fix (certifi; Spring Boot 3.4.4 -> 3.5.16; a Tomcat patch override), then gated only on
   a CRITICAL that HAS a fix. Unfixable findings are reported, never gating.
4. Each exception names image + package + CVE + a reason, and expires within 120 days. The
   gate fails on an expired exception AND on a stale one that matches nothing - so the list
   stays true in both directions. The expiry forces the real decision (here: Spring Boot 4).
5. Three things, all of the same kind: claiming without checking. See the 2-minute answer.
6. Push signed images by digest, deploy with environment approvals, Dependabot/Renovate for
   the SHA pins and base-image digests, SBOMs, org-wide required checks.

## 2-minute answers
**Q5 (what I got wrong).** (a) A green Java job proved nothing: `mvn -q` hides the test count,
so "0 tests ran" would also be green. I added a step that sums the surefire reports and fails
on 0 tests or any skipped test - then it showed 23. (b) Trivy said Tomcat CVEs were "fixed in
10.1.58". I overrode to 10.1.58 without being able to check Maven Central; the build failed -
10.1.58 was never published (10.1.59-61 were). Then I wrote an exception saying "fix not
published yet" - also unchecked, also wrong. The fix was 10.1.61, checked in maven-metadata.xml
first. Lesson: an advisory's "fixed version" is a claim, not an artifact. (c) My own command
block let a failed build continue to commit and push; every block is now chained with `&&`.

**Q1 follow-up (a flaky test).** A Kafka integration test failed once (`None == [0, 1, 2]`).
The fixture created a topic and the test read metadata immediately; `create_topics()` returns
before the metadata lists it. A re-run would have been green and hidden the race. The fixture
now waits until every partition is visible (max 15 s) and fails loudly if it never appears.
And a process lesson: that fix first missed its PR (I re-packaged after the owner had applied
the first package), so a green run was luck while I claimed the fix held. The base-hash check
on the next package caught it - checking what is ACTUALLY on main beats remembering it.

**What I would say I did NOT do.** No image push, no deploy, no environments (owner decision:
nothing consumes a pushed image yet). Base images are pinned by tag, not digest. No arm64
build. And Spring Boot 3.5 has had no release since 2026-06-25: staying on 3.x now costs a
manual override or exception per new CVE - that is the case for Boot 4, due before the
exceptions expire on 2027-01-31.
