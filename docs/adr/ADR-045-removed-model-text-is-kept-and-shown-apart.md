# ADR-045: Model text that code removes is kept, with the reason, and shown apart from the report

Status: Proposed (29c built 2026-10-28; sandbox only)
Date: 2026-10-28
Refines: ADR-022 (postmortem checks), ADR-021 (ranker explanations), ADR-043 (demo step 7:30)

## Context
- Code already removes model sentences: the postmortem check drops a sentence that cites no
  observation, has link/markup syntax, or has a number that is not in the facts it cites; the
  ranker's explanation is dropped when it cites no observation. Only COUNTS were stored (the
  ranker's drop not even that). A reader could not see what was taken out, or why.
- The Phase 1 demo script promised "one unsupported sentence removed (shown in diff)".
- Found while building: an action item of 3-4 characters passed the sentence check and was then
  dropped by a second length rule without being counted. An action item with a category outside
  the enum is refused by the LLM gateway's schema check before the agent sees it (so it is not a
  real gap); the agent's own filter is now defensive and counted.

## Decision
- The postmortem check returns every sentence it does not keep in `removed`: section, the model's
  text (as data), the FIRST rule it broke (markup, too_short, no_valid_ref, no_evidence,
  unknown_number; bad_category defensive) and a code-written detail (the refs that do not exist,
  the numbers not in the facts). `sentences_dropped` and `numbers_rejected` are DERIVED from that
  list, so the counts cannot disagree with it. Stored in `incident.postmortems.content`
  (JSONB: no migration).
- The ranker's explanation without a valid ref is kept as `explanation_removed` + why, in the
  hypothesis detail. The critic and the postmortem writer never see it (blanked in the critic's
  blind copy in the orchestrator AND in the critic agent).
- **Who may read it:** a removed text cites nothing valid, so it may quote ANY evidence the model
  was shown. It is shown only to a reader who may read EVERY evidence kind stored for the
  incident (fail closed); otherwise the text and the detail are redacted and the section +
  reason stay. An ENGINEER/SRE/IC sees it; a MANAGER sees reasons only.
- **Where:** never inside a report section. The API returns `removed` (+ `removed_redacted`,
  `removed_kept` = false for postmortems written before 29c: counts only, said so). Markdown has
  a separate "Removed by code - NOT part of this report" section; the text is escaped AND bare
  URLs are broken (`://` -> `: //`, `www.` -> `www .`) because a removed sentence can be the
  very link a check refused. The console shows it struck through, folded away, as text.
- **Demo:** `removed_shown` checks the mechanism - listed == counted, none hidden from the SRE,
  shown apart in the markdown, none inside the report. Owner (2026-10-28): a run that removed
  nothing is a PASS that says "0 removed"; a bad sentence is never planted into real output.

## Tradeoffs
- Model text that FAILED a check is now stored and shown. Kept to: plain text, a separate
  section, per-reader redaction stricter than for kept sentences, and defanged markdown.
- "Every evidence kind of the incident" is a superset of what one model saw: some readers lose
  text they could have read. Chosen because the exact set is not stored per run.
- The demo may show "0 removed" on real models: the step proves the mechanism, not that Sonnet
  writes a bad sentence on cue. Fake models write no narrative, so a rehearsal always shows 0.
- The critic's own dropped citations stay a count (citations, not sentences).
