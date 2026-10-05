---
name: synthetic-data
description: Generate deterministic, fictional enterprise demo data (100+ services, 500+ incidents, 1000+ docs, logs, metrics, deployments, commits, PRs, runbooks, dependencies). Use when seeding or extending data/ for demos and tests.
argument-hint: "<dataset> <count>"
---
# Synthetic data: $ARGUMENTS

- Fictional company: **Northwind Cloud Systems** (no real company names, products, people or internal terms).
- Deterministic: `Faker` + `random.Random(seed)`; same seed ⇒ byte-identical output. Seed recorded in `data/MANIFEST.json`.
- Generators live in `scripts/synth/`; output to `data/generated/` (gitignored). Small curated samples go in `data/sample-*` (committed).
- Keep referential integrity: incidents reference real generated service ids, deployments, commits.
- Plant **known ground truth** for evaluation: e.g. 40 incidents whose labelled root cause is pool exhaustion, with the matching log/metric/deploy signals. Write the labels to `data/eval/`.
- Include adversarial docs (prompt injection, restricted security docs) under `data/sample-documents/adversarial/`.
