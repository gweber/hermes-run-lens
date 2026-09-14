# Changelog

## Unreleased

- Failing auxiliary tasks: `aux.failed` events from every profile's agent log (background
  review, title generation, context summaries, exhausted auxiliary fallbacks, the paid
  OpenRouter lane), one finding per profile/task/error signature at ≥3 in 24 h (paid lane ≥1),
  and `aux_notify: kanban` to hand each new one to a profile as a kanban card. Off by default.
- `hermes lens watch --dry-run`.
- A capped run is reported once: the live hook's finding for a compressed child session is merged
  into the run's finding (one ops run on 2026-09-14 was reported twice).
- Aux-failure cards tell the worker to check recent commits first and to report by ~60 calls or
  block; the first card cost a 250-call run to rediscover a fix that was already committed.

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
