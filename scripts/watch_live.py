#!/usr/bin/env python3
"""Surveille la branche `live` écrite par le traqueur et imprime une ligne par événement.

Pensé pour l'outil Monitor de Claude Code (chaque ligne = une notification),
mais utilisable à la main : `python3 tracker/watch_live.py --once`.
Écrit aussi un instantané compact pour le tableau de bord (--dash-out).
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    PARIS = ZoneInfo("Europe/Paris")
except Exception:
    PARIS = timezone(timedelta(hours=2))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if s else None


def hhmm(s):
    return parse_iso(s).astimezone(PARIS).strftime("%H:%M") if s else "—"


def git(*args):
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)


def read_live():
    r = git("fetch", "-q", "origin", "+refs/heads/live:refs/remotes/origin/live")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])
    status = json.loads(git("show", "origin/live:status.json").stdout)
    hist = []
    for line in git("show", "origin/live:history.jsonl").stdout.splitlines():
        try:
            hist.append(json.loads(line))
        except ValueError:
            pass
    return status, hist


def dashboard_doc(s, hist):
    step = max(1, math.ceil(len(hist) / 150))
    pts = hist[::step]
    if hist and pts[-1] is not hist[-1]:
        pts.append(hist[-1])
    return {
        "updated_at": s["updated_at"],
        "phase": s["phase"],
        "phase_label": s["phase_label"],
        "landed": s["landed"],
        "landed_at": s["landed_at"],
        "landed_source": s["landed_source"],
        "at_gate": s["at_gate"],
        "at_gate_at": s["at_gate_at"],
        "eta": s["eta"],
        "departure": s["departure"],
        "position": s["position"],
        "aircraft": s["aircraft"],
        "airport": s["airport"],
        "fr24_text": s.get("fr24_text"),
        "events": s["events"][-12:],
        "sources": {k: v["ok"] for k, v in s["sources"].items()},
        "run_url": (s.get("run") or {}).get("url"),
        "history": [{"t": p["t"], "eta": p["eta"], "dist": p.get("dist"), "alt": p.get("alt")} for p in pts],
        "pushed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="/tmp/watch_live_state.json")
    ap.add_argument("--dash-out", default=None)
    ap.add_argument("--interval", type=int, default=45)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    try:
        with open(a.state) as f:
            mem = json.load(f)
    except (FileNotFoundError, ValueError):
        mem = {}

    def save():
        with open(a.state, "w") as f:
            json.dump(mem, f)

    def emit(msg):
        print(msg, flush=True)

    while True:
        try:
            s, hist = read_live()
            t = datetime.now(timezone.utc)
            if a.dash_out:
                with open(a.dash_out, "w") as f:
                    json.dump(dashboard_doc(s, hist), f, ensure_ascii=False)
            eta, src = s["eta"]["best"], s["eta"]["best_source"]
            summary = f"{s['phase_label']} · ETA {hhmm(eta)} ({src})"
            p = s.get("position") or {}
            if p:
                summary += f" · {p.get('dist_km')} km, {p.get('alt_ft')} ft"
            refresh_every = 300 if s["phase"] in ("descent", "approach", "landed") else 1200
            due = t.timestamp() - mem.get("last_emit", 0) > refresh_every

            if s["landed"] and not mem.get("landed"):
                mem["landed"] = s["landed_at"]
                emit(f"LANDED {hhmm(s['landed_at'])} ({s['landed_source']}) · terminal "
                     f"{s['airport'].get('terminal')} porte {s['airport'].get('gate')} "
                     f"tapis {s['airport'].get('baggage')}")
            elif s["at_gate"] and not mem.get("at_gate"):
                mem["at_gate"] = s["at_gate_at"]
                emit(f"AT_GATE {hhmm(s['at_gate_at'])} · {summary}")
            elif s["phase"] != mem.get("phase"):
                emit(f"PHASE {summary}")
            elif mem.get("eta") and eta and abs((parse_iso(eta) - parse_iso(mem["eta"])).total_seconds()) >= 300:
                emit(f"ETA_CHANGE {hhmm(mem['eta'])} -> {hhmm(eta)} · {summary}")
            elif t - parse_iso(s["updated_at"]) > timedelta(minutes=8):
                if t.timestamp() - mem.get("last_stale", 0) > 900:
                    mem["last_stale"] = t.timestamp()
                    emit(f"STALE dernière mise à jour du traqueur à {hhmm(s['updated_at'])}")
            elif due:
                emit(f"REFRESH {summary}")
            else:
                if a.once:
                    emit(f"OK {summary}")
                time.sleep(a.interval)
                continue
            mem.update(phase=s["phase"], eta=eta, last_emit=t.timestamp())
            save()
        except Exception as e:  # réseau, branche absente…
            if time.time() - mem.get("last_err", 0) > 900:
                mem["last_err"] = time.time()
                save()
                emit(f"ERROR {e}")
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
