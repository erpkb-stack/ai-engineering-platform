# Interview prep — Phase 15: release risk analysis

## Q1. How accurate is your release risk score?
I don't claim any accuracy. The synthetic deploys are not linked to incidents, so there is no
ground truth - a number would be invented. What I measure instead is **selectivity**: over all
1,784 synthetic deploys the rule says HIGH twice (exactly the two planted bad changes) and LOW
for 94 %. And I lock behaviour with 6 hand-labelled cases. I say plainly: the only risky code in
that history is planted, so this proves the rule is quiet, not that it is right on real code.
Accuracy needs outcome data - that is Phase 25.

## Q2. Why not a 0-100 score, or an LLM judgment?
A score needs weights, and I have no data to fit them - it would look precise and be a guess. A
model's judgment cannot be checked. A band over named factors, each citing a commit or a
catalog entry, can be argued with line by line.

## Q3. What did the data teach you about the rule?
Two things. First, if a changed timeout counted as risk, 41 % of releases would be flagged -
nobody reads a warning like that, so it is context. Second, "unknown is not safe" flagged 102
releases only because the first deploy in the data window had no base. I kept the rule but
split the report: `findings_band` (what we found) and `coverage` (what we could see).

## Q4. What is the "way back" and why does it matter?
In Phase 14 the model suggested flipping a feature flag as mitigation - but the risky code was
not behind that flag. Now code decides: if the risky call is inside ONE positive flag check,
switching the flag off removes it; otherwise only a redeploy does; and a flag the release
changed that the code never reads gets an explicit "switching it back does NOT undo the code".
A review found the first version accepted `!flags.isEnabled(x)` as a guard - inverted advice.
Now only a single positive check counts; anything else falls back to the safe answer.

## Q5. It is synchronous - doesn't your API rule say long work is 202?
It is code-only and bounded: about five tool calls, a 10 s deadline per call budget, 12 s at
the orchestrator, 15 s at the api. A 202 would add a status resource for seconds of work. I
wrote that exception into the ADR. A slow tool gives a PARTIAL report, not a timeout.

## Weak spots to admit
- `compare_commits` has no repository ACL (latent: all synthetic repos are shared).
- Flag detection is by name; a flag read into a variable first is not seen (safe side).
- Synthetic code: real code would fire the rules more often; selectivity must be re-measured.
