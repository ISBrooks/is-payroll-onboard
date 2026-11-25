"""One-button payroll run: sync -> book adjustments -> period statement.

Spawned by the dashboard's "Lock & run payroll" button (Payroll ->
Downloads), or run by hand. Progress lands in data/payroll_run.json so the
dashboard can show a live status line:

  {"state": "running"|"done"|"failed", "step": "sync"|"adjustments"|
   "statement", "period_end": "...", "pid": N, "started": ..., "updated":
   ..., "totals": "...", "error": "..."}

  python scripts/run_payroll.py --period-end 2026-10-10
  python scripts/run_payroll.py --period-end 2026-10-10 --skip-sync
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS = ROOT / "data" / "payroll_run.json"

for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write(st: dict) -> None:
    st["updated"] = now()
    STATUS.write_text(json.dumps(st))


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "posix":
        return Path(f"/proc/{pid}").exists()
    import ctypes                              # NEVER os.kill(pid, 0) on
    h = ctypes.windll.kernel32.OpenProcess(    # Windows — that terminates it
        0x1000, False, pid)
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False


def already_running() -> bool:
    try:
        cur = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return False
    return cur.get("state") == "running" and pid_alive(cur.get("pid"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period-end", required=True, help="Saturday YYYY-MM-DD")
    ap.add_argument("--skip-sync", action="store_true",
                    help="use current data (re-runs after a same-day sync)")
    ap.add_argument("--allow-pre-go-live", action="store_true",
                    help="regenerate a pre-go-live period's parallel-run statement "
                         "(the manual sheet paid it)")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT / "src"))
    from commissions.config import load_settings
    live_from = load_settings().payroll_live_from
    if args.period_end < live_from and not args.allow_pre_go_live:
        print(f"{args.period_end} is before payroll go-live ({live_from}) — the "
              "manual sheet paid it; not running. Pass --allow-pre-go-live to "
              "regenerate its parallel-run statement on purpose.")
        return 2

    if already_running():
        print("a payroll run is already in progress — not starting another")
        return 1

    st = {"state": "running", "pid": os.getpid(),
          "period_end": args.period_end, "step": "sync", "started": now()}
    py = sys.executable
    steps = []
    if not args.skip_sync:
        steps.append(("sync", [py, "scripts/sync.py"]))
    steps.append(("adjustments", [py, "scripts/form_intake.py"]))
    steps.append(("statement",
                  [py, "scripts/report_payroll.py", "--period-end",
                   args.period_end]))

    for name, cmd in steps:
        st["step"] = name
        write(st)
        print(f"== {now()} {name}: {' '.join(cmd[1:])}", flush=True)
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        print(r.stdout, flush=True)
        if r.stderr:
            print(r.stderr, file=sys.stderr, flush=True)
        if r.returncode != 0 and name == "adjustments":
            # if the Google Form's sheet can't be read, still book the
            # dashboard queue and keep the run alive
            print("full intake failed — retrying dashboard queue only", flush=True)
            r = subprocess.run([py, "scripts/form_intake.py", "--queue-only"],
                               cwd=ROOT, capture_output=True, text=True)
            print(r.stdout, flush=True)
        if r.returncode != 0:
            st.update(state="failed",
                      error=f"{name} exited {r.returncode}: "
                            f"{(r.stderr or r.stdout or '')[-400:]}")
            write(st)
            return r.returncode
        if name == "statement":
            m = re.search(r"TOTAL\s+(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)",
                          r.stdout)
            if m:
                st["totals"] = (f"${m.group(1)} / -${m.group(2).lstrip('-')} "
                                f"/ net {m.group(3).replace('-', '-$') if m.group(3).startswith('-') else '$' + m.group(3)}")
            # RULES §7 weekly split: the two weeks' totals for the run card
            wk = re.findall(r"Week ([12]) \(([\d/]+)-([\d/]+)\): commissions (-?[\d,.]+) "
                            r"deductions (-?[\d,.]+) total (-?[\d,.]+)", r.stdout)
            if len(wk) == 2:
                st["week_totals"] = " · ".join(
                    f"week {n} ({a}–{b}): ${c} / -${d.lstrip('-')} / net "
                    f"{'-$' + t[1:] if t.startswith('-') else '$' + t}"
                    for n, a, b, c, d, t in wk)

    st.update(state="done", step="done")
    write(st)
    print(f"== {now()} payroll run complete for {args.period_end}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
