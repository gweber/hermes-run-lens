# Security

run-lens watches agents that run shell commands, read files and call models on your
behalf, and it can stop them. So the questions that matter are what it keeps, what it
reads, what it sends, and who can press stop.

## Reporting

Open an issue. There is no private disclosure channel and no bounty; this is a
single-maintainer plugin for a self-hosted agent.

## Threat model

- **Trusted:** the operator, the machine, the OS user Hermes runs as, the Hermes
  config, the LiteLLM proxy if there is one.
- **Untrusted:** everything an agent handles — tool arguments, tool results, model
  output. They are derived from conversations, web pages and files and must be treated
  as attacker-influenced even though they end up in a local database.
- **The Hermes dashboard's authentication is the perimeter** for everything the plugin
  exposes over HTTP. The plugin adds routes under `/api/plugins/run-lens/` and nothing
  outside the dashboard.

## What the store keeps

`<default home>/plugin-data/run-lens/lens.db`, created owner-only (0600).

- **Kept:** session ids, titles, profile, source, model names; per call: token counts,
  latency, time to first token, finish reason, error type; per tool call: tool name, a
  hash of the arguments, **a redacted preview of the arguments and of failed results**
  (240 characters by default, `preview_chars`), status and duration; findings; stop
  requests.
- **Not kept:** prompts, system prompts, conversation messages, full tool results, API
  keys.
- **Redaction:** previews go through Hermes's own `agent.redact.redact_sensitive_text`
  with `force=True` (API keys, bearer tokens, JWTs, private keys, credentials in URLs and
  connection strings). Outside a Hermes install a smaller local pattern set is used.
  Redaction is pattern-based; a secret in an unusual shape can survive in a preview.
- **Retention:** per-call detail is pruned after `retention_days` (90); sessions and
  findings are kept until the file is deleted.

## What it reads

Hermes's own files under the Hermes home (`state.db` of every profile, `logs/agent.log*`,
`cron/`, kanban databases), read-only. LiteLLM spend logs, only when configured or
detected:

- the admin API with a key taken from an **environment variable whose name** is
  configured — the key itself is never written to config or to the store;
- the proxy's Postgres with a DSN from an environment variable;
- `docker exec` into a named container, which requires the Hermes user to be allowed to
  use Docker — itself root-equivalent on most hosts. Prefer the API or DSN sources where
  that matters.

The watermark interpolated into the spend-log SQL comes from the plugin's own store and is
checked against a strict timestamp pattern first.

## What it sends

- **To a LiteLLM proxy** (only a detected or configured one, `tag_litellm`): the Hermes
  session id, call id, profile name and platform in `metadata.spend_logs_metadata` of each
  request. LiteLLM stores these in its own database. Set `tag_litellm: off` if profile
  names or session ids must not end up there.
- **To a notification target** (only if `hermes lens setup` was run): finding titles and
  suggestions, through Hermes's own cron delivery.
- **To an OpenTelemetry collector** (only with `hermes lens export`): ids, model names and
  numbers; no previews.

Nothing else leaves the machine.

## Who can stop a run

- Anyone who can write the store can request a stop — the same OS user that owns the
  Hermes home, who could equally kill the process.
- Anyone authenticated to the Hermes dashboard can press Stop on a live run and change a
  finding's state.
- The **breaker** stops runs on its own. It is armed for cron sessions by default and only
  on repeated identical failures, repeated identical replies, or a run far past its
  group's normal size. `breaker: off` disables it.
- A stop interrupts only an agent whose session id matches, found in the memory of the
  process that owns it; it cannot reach other processes or other users' agents.

## Failure behaviour

Every hook is fail-open: an exception is logged and the agent continues. The writer thread
drops rows rather than block. A slow or unreachable LiteLLM proxy is probed from the
writer thread with a two-second timeout, never from an agent's thread.
