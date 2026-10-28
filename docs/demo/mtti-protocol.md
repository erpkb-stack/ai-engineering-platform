# MTTI protocol - read this BEFORE you start the clock (Phase 29e, ADR-047)

MTTI here = time to IDENTIFY the root cause of a planted incident: AI vs you, same data, same
read tools. The rules are fixed in `tests/mtti/mttilib.py` and do not change after timing starts.

## The AI side (one paid run, about $1.30)
1. Deploy with the real routing: `make demo-fresh` (no `LLM_ROUTING=`).
2. `make eval-investigations COST=1` (v1, 16 cases) and `make eval-investigations VERSION=2 COST=1`
   (v2, 6 cases). Keep the two result files in `data/eval/results/`.
3. The AI identified a case only if its top cause is the planted one AND it is VALIDATED. Its time
   = incident created -> investigation finished.

## Your side (`make manual`, 6 cases, one at a time)
- **Before:** do not open `eval.eval_cases`, the scenario code (`scripts/synth`), old eval result
  files or the console's incidents for these services. You built this data with Claude: your
  times are an optimistic baseline anyway; do not make them more so.
- **The sample is fixed:** 4 cases from v1 + 2 from v2, chosen by a seed. v1-S01 is left out (it
  is the demo's own pattern). You do not choose and you cannot skip.
- **The clock** starts when you press Enter on a case (you see only the alert) and stops at
  `answer`. It does NOT pause: `quit` keeps it running. Do one case in one sitting.
- **Tools:** only what `make manual` offers (logs, metrics, deploys, config diff, runbooks, past
  incidents, catalog, `tool ...`) - the read tools the agents use, as you (an SRE token). No
  console, no SQL, no Grafana, no notes from earlier cases.
- **Answer** with `answer <kind> <subject>`: kind = change | saturation | traffic | alternative;
  subject = the DEPLOY key (DEPLOY-9100) or the metric (db_pool_utilization). One answer per
  case: it is final.
- **Cap:** 30 minutes. A right answer after 30 minutes counts as not identified (the cap is never
  used as a time).
- **Interrupted for real** (a call, a meeting): `abandon <reason>`. The case is listed as
  abandoned, never scored, and a reserve case of the same mix comes next. Do not abandon a case
  because it is hard: the report lists every abandon with its reason.

## The report (`make mtti FILES="<v1 file> <v2 file>"`)
Reveals the planted answers, then prints per case your time and the AI's, identified or not, and:
identified rates with Wilson 95% intervals, median times with min-max (only identified cases),
and the paired result on your 6 cases (who identified first, exact sign test). With 6 pairs the
smallest possible p is 0.031: this is a small sample, and it is quoted as one - never as
"X% faster".
