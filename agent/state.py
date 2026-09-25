"""Small JSON state file (persisted between GitHub Actions runs with actions/cache)."""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone

DEFAULT = {
    "version": 1,
    "next_id": 1,
    "open_signals": [],
    "closed_signals": [],
    "last_eval_bar": {},
    "last_close": {},
    "news": {"seen": {}, "items": []},
    "calendar": {"fetched_at": None, "events": [], "alerted": []},
    "tg_offset": None,
    "llm_calls": {},
    "calibration": {},
    "cmd_brief": {},
    "last_error_notice": None,
    "started": None,
}


def _json_default(o):
    if isinstance(o, float) and math.isinf(o):
        return 999.0
    raise TypeError(str(type(o)))


def load(state_dir: str) -> dict:
    path = os.path.join(state_dir, "state.json")
    st = json.loads(json.dumps(DEFAULT))
    if os.path.exists(path):
        try:
            with open(path) as f:
                st.update(json.load(f))
        except (OSError, ValueError):
            pass
    st["started"] = st.get("started") or datetime.now(timezone.utc).isoformat()
    return st


def save(state_dir: str, st: dict, now: datetime | None = None) -> None:
    os.makedirs(state_dir, exist_ok=True)
    prune(st, now)
    path = os.path.join(state_dir, "state.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, default=_json_default)
    os.replace(tmp, path)


def prune(st: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=72)).isoformat()
    st["news"]["seen"] = {k: v for k, v in st["news"]["seen"].items() if v >= cutoff}
    st["news"]["items"] = [i for i in st["news"]["items"] if i["published"] >= (now - timedelta(hours=48)).isoformat()]
    st["closed_signals"] = st["closed_signals"][-1000:]
    st["calendar"]["alerted"] = st["calendar"]["alerted"][-200:]
