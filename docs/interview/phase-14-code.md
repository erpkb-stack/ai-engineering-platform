# Interview prep — Phase 14: code intelligence

## Q1. Why no LLM to read the diff?
- Every claim the agent makes is a rule I can test: "a database call is in a loop body now
  and was not before". A model could invent a mechanism, and I could not check it.
- Rules run on the file before and after the change. I rebuild the "before" file from the
  diff, strictly: if the diff does not fit the file, the file is reported as unchecked.
- Cost: no tokens. Limit: rules only know the patterns I wrote (loop queries, remote calls,
  limits, removed fields).

## Q2. How much does a code finding change the conclusion?
- It is ONE more source for the deploy that shipped it - never a band override.
- Only a loop query or loop remote call counts. A changed timeout does not: in the synthetic
  history that rule fired on 810 commits - routine tuning, not evidence.
- It counts only when the deploy already has a symptom on its own service. Code proves the
  mechanism exists; the symptom shows it fired. The first build skipped this gate and the
  review showed an unrelated deploy moving from LOW to MEDIUM.

## Q3. Code is restricted per repository. How do you not leak it?
- The index stores the repository's groups on every file; "who uses X" filters by the
  caller's groups in the SQL, in the main query and in the type sub-query, plus an assert.
- What is stored on the incident is visible per ROLE, not per repo group. So the agent keeps
  only references in code every engineer may read.
- The investigation plan and its error message are readable by every incident reader: they
  hold counts only. The first build put symbols there; the review caught it.
- Honest gap: `compare_commits` has no repository ACL (all synthetic repos are shared). A real
  code host must enforce it there.

## Q4. How do you know the rules are right?
- Unit cases for every false positive and miss the review found (loop header, re-indented
  line, removed function parameter, Go tags, Java annotations), each with a mutation check.
- Over all 3,602 synthetic diffs the rules find exactly the two planted changes (plus limit
  changes). That is precision on synthetic code, not on real code - said so in the ADR.

## Q5. A redeploy or a rollback - what does the agent say?
Nothing shipped. `compare_commits` returns `same` or `rollback` and no commits; the agent notes
it. The first build analysed the old head commit as "new code" - a review finding.

## Weak spots to admit
- Simple names, no type resolution; calls inside lambdas/streams are not seen.
- Commits ordered by time, not the merge graph.
- Synthetic code: one template per file kind; real code would need per-framework rules and a
  labelled set of real N+1 PRs.
