---
name: interview-prep
description: Turn a completed AEOI component into Senior/Staff AI Engineer interview material in the required 6-part format. Use at the end of every phase or when the user asks "how do I explain X in an interview".
argument-hint: "<topic or phase>"
---
# Interview prep: $ARGUMENTS

For each question produce exactly:
1. **30-second answer** — one decision, one reason, one tradeoff. ~75 words.
2. **2-minute answer** — context → decision → how it works → what breaks → what I'd do at 10× scale.
3. **Architecture explanation** — reference real files in this repo (path + function).
4. **Tradeoffs** — at least one where I chose the *less* impressive option on purpose.
5. **Common mistakes** — what weak candidates say.
6. **Follow-up questions** — 3 the interviewer will ask next, with one-line answers.

Rules: only cite metrics measured in this repo (link the eval run / Grafana panel). If none exist, say "not yet measured" — never invent. Write to `docs/interview/<topic>.md`. Use plain English (the owner speaks English as a second language — short sentences, no idioms).
