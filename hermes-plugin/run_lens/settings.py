"""Settings — declared in plugin.yaml's `config_schema`, set in config.yaml.

    plugins:
      entries:
        run-lens:
          settings:
            breaker: cron
            litellm_spend_source: docker
            litellm_docker_container: litellm-db

Hermes hands a plugin its settings through `ctx.get_config`. The CLI, the
ingesters and the dashboard backend run without a plugin context, so they read
the same block from the root home's config.yaml. The root home, because run-lens
keeps one store for the whole install (see paths.py); a profile's own
`plugins.entries.run-lens.settings` override the root for that profile's live
recorder only.
"""
from __future__ import annotations

from typing import Any

from . import paths

PLUGIN_ID = "run-lens"

# Keep in step with config_schema in plugin.yaml.
DEFAULTS: dict[str, Any] = {
    # live breaker: which sessions it may stop, and at which signal
    "breaker": "cron",                    # off | cron | cron+kanban | all
    "breaker_exact_failures": 12,
    "breaker_identical_output": 25,
    "breaker_calls_floor": 80,
    "breaker_calls_factor": 6.0,
    # LiteLLM
    "tag_litellm": "auto",                # auto | on | off
    "litellm_url": "",                    # empty: every provider base_url that answers like LiteLLM
    "litellm_spend_source": "auto",       # auto | api | postgres | docker | off
    "litellm_admin_key_env": "LITELLM_MASTER_KEY",
    "litellm_postgres_dsn_env": "LITELLM_DATABASE_URL",
    "litellm_docker_container": "",
    "litellm_backfill_days": 14,
    # store
    "store_path": "",
    "retention_days": 90,
    "preview_chars": 240,
    # watch job
    "watch_schedule": "*/5 * * * *",
    "watch_deliver": "local",
    "watch_severity": "high",
    "watch_window": "3h",
}

_cache: dict[str, Any] | None = None


def _read_block(home) -> dict:
    try:
        import yaml  # type: ignore

        cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    entry = (((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID)) or {}
    out = {}
    for key in ("config", "settings"):  # Hermes reads settings first, then config
        block = entry.get(key)
        if isinstance(block, dict):
            out.update(block)
    return out


def load(refresh: bool = False) -> dict[str, Any]:
    """Defaults, overlaid with the root home's settings."""
    global _cache
    if _cache is None or refresh:
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in _read_block(paths.root_home()).items() if v is not None})
        _cache = merged
    return dict(_cache)


def from_ctx(ctx) -> dict[str, Any]:
    """Settings as the plugin context sees them (the active profile's config)."""
    out = load()
    for key in DEFAULTS:
        try:
            value = ctx.get_config(key, None)
        except Exception:
            value = None
        if value is not None:
            out[key] = value
    return out


def get(key: str) -> Any:
    return load().get(key, DEFAULTS.get(key))
