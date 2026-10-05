---
name: adr
description: Create or supersede an Architecture Decision Record in docs/adr/. Use whenever a technology choice, data model, protocol or boundary decision is made or changed.
argument-hint: "<short title>"
---
# Write an ADR: $ARGUMENTS

1. Find the next number: `ls docs/adr | sort | tail -1`.
2. Copy `docs/adr/ADR-000-template.md` to `docs/adr/ADR-NNN-<kebab-title>.md`.
3. Fill every section: Context, Decision, Alternatives (≥2, each with why-not), Tradeoffs, Consequences,
   **Prototype vs Production vs Enterprise-scale**, **When we would revisit**.
4. Status starts as `Proposed`. Only the user moves it to `Accepted`.
5. Accepted ADRs are immutable (a hook enforces this). To change one: new ADR with `Supersedes: ADR-XXX`, and set the old one's status line to `Superseded by ADR-NNN` (ask the user — the hook will block the edit; the user does it).
6. Add a row to the ADR index in `docs/adr/README.md`.
Be adversarial: include the strongest argument *against* the decision in Tradeoffs.
