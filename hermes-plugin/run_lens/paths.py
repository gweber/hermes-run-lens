"""Where run-lens reads from and writes to.

One store for the whole Hermes install, not one per profile — the one deliberate
departure from Hermes's `plugin_data_dir`, which follows the active profile. The
reason is how Hermes runs: in a multiplexed gateway a secondary profile's cron run
executes in the default profile's process, logs to the default agent.log and is
stored in the default state.db, while a kanban worker for another profile writes to
that profile's state.db from its own process. "What is using the model right now"
only has an answer when every process writes to the same place, so the store lives
under the root home's `plugin-data/run-lens/`, whichever profile records.

Profiles are enumerated with Hermes's own `hermes_cli.profiles.list_profiles()`
where it is importable, and by scanning `profiles/` otherwise.
"""
from __future__ import annotations

import os
from pathlib import Path


def active_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def root_home() -> Path:
    """The default profile's home — the directory that holds `profiles/`."""
    env = os.environ.get("RUN_LENS_HERMES_ROOT")
    if env:
        return Path(env)
    home = active_home()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def db_path() -> Path:
    env = os.environ.get("RUN_LENS_DB")
    if env:
        return Path(env).expanduser()
    try:
        from . import settings

        configured = settings.get("store_path")
    except Exception:
        configured = ""
    if configured:
        return Path(str(configured)).expanduser()
    return root_home() / "plugin-data" / "run-lens" / "lens.db"


def profile_name(home: Path | None = None) -> str:
    home = Path(home) if home is not None else active_home()
    if home.parent.name == "profiles":
        return home.name
    return "default"


_homes_cache: dict = {"at": 0.0, "root": None, "value": None}


def homes() -> list[tuple[str, Path]]:
    """(profile, home) for the default profile and every named profile (cached 60 s)."""
    import time

    root = root_home()
    c = _homes_cache
    if c["value"] is not None and c["root"] == root and time.time() - c["at"] < 60:
        return list(c["value"])
    value = _homes(root)
    c.update(at=time.time(), root=root, value=value)
    return list(value)


def _homes(root: Path) -> list[tuple[str, Path]]:
    if not os.environ.get("RUN_LENS_HERMES_ROOT"):
        try:
            from hermes_cli.profiles import list_profiles  # type: ignore

            found = [("default" if p.is_default else p.name, Path(p.path)) for p in list_profiles()]
            if found:
                found.sort(key=lambda x: (x[0] != "default", x[0]))
                return found
        except Exception:
            pass
    out = [("default", root)]
    pdir = root / "profiles"
    if pdir.is_dir():
        for p in sorted(pdir.iterdir()):
            if p.is_dir() and (p / "config.yaml").exists():
                out.append((p.name, p))
    return out
