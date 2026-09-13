"""Dump API responses from a run-lens store for the dashboard render check.

    python tests/dump_fixtures.py --demo OUT.json          # synthetic store (CI)
    RUN_LENS_DB=/path/lens.db python tests/dump_fixtures.py OUT.json

With a real store the fixtures come from the actual read models, so the render check
exercises the shapes the dashboard really receives — nulls, empty baselines, very long
runs. That output holds real session titles and tool arguments: keep it out of git
(*fixtures*.json is ignored).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hermes-plugin"))
sys.path.insert(0, str(HERE.parent / "hermes-plugin" / "dashboard"))
_HERMES = os.environ.get("HERMES_AGENT_DIR") or str(Path.home() / ".hermes" / "hermes-agent")
if Path(_HERMES).is_dir():
    sys.path.insert(0, _HERMES)

args = sys.argv[1:]
demo = "--demo" in args
args = [a for a in args if a != "--demo"]
if demo:
    tmp = tempfile.mkdtemp()
    os.environ["RUN_LENS_DB"] = str(Path(tmp) / "demo.db")
    os.environ["RUN_LENS_HERMES_ROOT"] = tmp
    from run_lens import demo as _demo

    _demo.build(os.environ["RUN_LENS_DB"])

import plugin_api as api  # noqa: E402

api._maybe_refresh = lambda force=False: None  # read what is stored, do not ingest
out = {
    "/overview": api.overview("3d"),
    "/runs": api.runs("3d", limit=300),
    "/jobs": api.jobs("7d"),
    "/models": api.models("3d"),
    "/findings": api.findings("open", "info", 300),
}
runs = out["/runs"]["runs"]
heavy = sorted(runs, key=lambda r: -(r["calls"] or 0))
picked = [heavy[0]["root_id"]] if heavy else []
picked += [r["root_id"] for r in runs[:2]]
out["/run"] = {rid: api.run(rid) for rid in dict.fromkeys(picked)}
Path(args[0]).write_text(json.dumps(out, default=str))
print(f"wrote {args[0]}: {len(runs)} runs, {len(out['/run'])} run details")
