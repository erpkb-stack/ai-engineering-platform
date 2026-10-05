# tests

unit/ integration/ security/ evaluation/ e2e/ (+ load/ and chaos/ added in Phases 27–28).
Rules: `.claude/rules/testing.md`. Use the `test-engineer` subagent and `security-test` skill.
Markers: `-m unit` (default), `-m integration` (needs Docker), `-m eval` (real LLM, costs money), `-m e2e`.
