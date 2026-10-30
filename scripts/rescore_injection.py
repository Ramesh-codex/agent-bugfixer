"""Offline replay of logged trajectories through the injection-flag detector.

Re-derives `injection_flag` for every bug in one or more `reports/runs/<id>/`
directories directly from the already-logged JSONL trajectories -- no API
calls, no sandbox, no live agent run. Useful for checking the effect of a
detector change against runs that already happened.

Replays the same three checks `agent.loop.run_agent_loop` applies live:
  1. `_looks_like_env_dump` on each turn's model-response text.
  2. a per-tool-call check on each tool call's arguments.
  3. `_looks_like_env_dump` on the `finish` call's `summary`.

For (2), "before" is the pre-fix behavior: `_args_touch_env` applied
unconditionally, including to `finish`'s own `summary` argument -- the
substring-match false positive described in FINDINGS.md F5. "after" is the
current, shipped `_tool_call_touches_env`, which skips `finish`.

    python scripts/rescore_injection.py reports/runs/20261029_184512 reports/runs/20261029_185026
"""

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from agent.loop import _args_touch_env, _looks_like_env_dump, _tool_call_touches_env

ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "reports" / "injection_flag_rescore.json"


@dataclass
class RescoreRow:
    run_id: str
    bug_id: str
    injection_flag_before: bool
    injection_flag_after: bool
    changed: bool


def _before_touches_env(name: str, args: dict) -> bool:
    """Pre-fix call site: no exclusion for `finish`."""
    return _args_touch_env(args)


def _replay(path: Path, touches_env) -> bool:
    flag = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        kind = event.get("event")

        if kind == "model_response":
            if _looks_like_env_dump(event.get("text", "")):
                flag = True

        elif kind == "tool_call":
            name = event.get("tool", "")
            args = event.get("args", {})
            if touches_env(name, args):
                flag = True
            if name == "finish" and not event.get("is_error", False):
                summary = args.get("summary", "")
                if _looks_like_env_dump(summary):
                    flag = True

    return flag


def rescore_run(run_dir: Path) -> list[RescoreRow]:
    rows = []
    for path in sorted(run_dir.glob("BUG-*.jsonl")):
        bug_id = path.stem
        before = _replay(path, _before_touches_env)
        after = _replay(path, _tool_call_touches_env)
        rows.append(
            RescoreRow(
                run_id=run_dir.name,
                bug_id=bug_id,
                injection_flag_before=before,
                injection_flag_after=after,
                changed=before != after,
            )
        )
    return rows


def print_table(rows: list[RescoreRow]) -> None:
    headers = ["Run", "Bug", "Before", "After", "Changed"]
    table_rows = [
        [r.run_id, r.bug_id, str(r.injection_flag_before), str(r.injection_flag_after), str(r.changed)]
        for r in rows
    ]
    widths = [max(len(h), *(len(row[i]) for row in table_rows)) for i, h in enumerate(headers)]

    def fmt(cols: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cols, widths))

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for row in table_rows:
        print(fmt(row))


def main() -> int:
    run_dirs = [Path(p) for p in sys.argv[1:]]
    if not run_dirs:
        print("usage: python scripts/rescore_injection.py <run_dir> [<run_dir> ...]", file=sys.stderr)
        return 1

    all_rows: list[RescoreRow] = []
    for run_dir in run_dirs:
        all_rows.extend(rescore_run(run_dir))

    print_table(all_rows)

    changed = [r for r in all_rows if r.changed]
    print(f"\n{len(changed)} of {len(all_rows)} rows changed.")
    for r in changed:
        print(f"  {r.run_id}/{r.bug_id}: {r.injection_flag_before} -> {r.injection_flag_after}")

    REPORT_PATH.write_text(json.dumps([asdict(r) for r in all_rows], indent=2), encoding="utf-8")
    print(f"\nWrote {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
