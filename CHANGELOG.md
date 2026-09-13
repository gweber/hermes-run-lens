# Changelog

## 0.2.0 — 2026-09-13

First public release.

- Live capture of LLM calls, tool calls, turns and sessions through Hermes hooks, in every
  agent process; one store for the whole install.
- Ingesters for `state.db`, `agent.log`, cron, kanban and LiteLLM spend logs (admin API,
  Postgres or Docker); LiteLLM proxies detected from the Hermes provider config.
- Exact LiteLLM joins through `metadata.spend_logs_metadata`.
- Group baselines and findings: caps, loops, runaways, prompt growth, silent fallbacks,
  model hogs, cron overlaps.
- `hermes lens stop` and a breaker that interrupt a run in its own process.
- `hermes lens` CLI, dashboard tab, `/lens`, `hermes lens setup` for a silent watch job,
  OpenTelemetry GenAI export, `hermes lens demo`.
- Settings declared in `config_schema`; retention; owner-only store.
