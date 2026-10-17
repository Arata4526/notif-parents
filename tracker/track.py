#!/usr/bin/env python3
"""Suivi en direct du vol AF902 Yaoundé (NSI) -> Paris CDG du 16-17 octobre 2026.

Tourne dans GitHub Actions : interroge plusieurs sources publiques toutes les
minutes, écrit l'état du vol dans la branche `live` (status.json, STATUS.md,
history.jsonl) et prévient à l'atterrissage (issue GitHub + ntfy).
Aucune dépendance : bibliothèque standard Python uniquement.
"""
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    PARIS = ZoneInfo("Europe/Paris")
except Exception:  # pas de base tz : heure d'été de Paris
    PARIS = timezone(timedelta(hours=2))

FLIGHT = "AF902"
CALLSIGNS = ["AFR902"]
CDG = (49.0097, 2.5479)
NSI = (3.7226, 11.5533)
# Horaires relevés sur parisaeroport.fr le 17/10 à 01:53 (UTC).
SCHED_DEP = "2026-10-16T21:25:00Z"  # 22:25 à Yaoundé
ANNOUNCED_DEP = "2026-10-16T22:35:00Z"  # 23:35 à Yaoundé
SCHED_ARR = "2026-10-17T04:35:00Z"  # 06:35 à Paris
ANNOUNCED_ARR = "2026-10-17T05:35:00Z"  # 07:35 à Paris

LIVE_DIR = os.environ.get("LIVE_DIR", "live-data")
POLL_S = int(os.environ.get("POLL_SECONDS", "60"))
MAX_MIN = float(os.environ.get("MAX_MINUTES", "345"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "")
MENTION = os.environ.get("NOTIFY_MENTION", "")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

ADSB_SOURCES = {
    "airplanes.live": "https://api.airplanes.live/v2",
    "adsb.lol": "https://api.adsb.lol/v2",
    "adsb.fi": "https://opendata.adsb.fi/api/v2",
}

PHASE_LABELS = {
    "scheduled": "Pas encore décollé",
    "airborne": "En vol",
    "cruise": "En croisière",
    "descent": "En descente vers Paris",
    "approach": "En approche finale",
    "landed": "Atterri à CDG",
    "at_gate": "Arrivé au terminal",
}


# --- utilitaires -----------------------------------------------------------

def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_iso(s):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def from_ts(ts):
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc) if ts else None
    except (TypeError, ValueError):
        return None


def hhmm(dt):
    return dt.astimezone(PARIS).strftime("%H:%M") if dt else "—"


def haversine_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def http(url, headers=None, data=None, method=None, timeout=20):
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url, **kw):
    return json.loads(http(url, **kw))


def mark(state, name, error=None):
    state["sources"][name] = {
        "ok": error is None,
        "at": iso(now()),
        "error": None if error is None else str(error)[:200],
    }


# --- sources ---------------------------------------------------------------

def fetch_adsb(state):
    """Position brute via les agrégateurs ADS-B ouverts ; garde la plus fraîche."""
    best = None
    callsigns = list(dict.fromkeys(CALLSIGNS + [state["aircraft"].get("callsign") or ""]))
    for name, base in ADSB_SOURCES.items():
        urls = [f"{base}/hex/{state['aircraft']['hex']}"] if state["aircraft"].get("hex") else []
        urls += [f"{base}/callsign/{cs}" for cs in callsigns if cs]
        try:
            for url in urls:
                d = get_json(url)
                acs = [ac for ac in (d.get("ac") or d.get("aircraft") or []) if ac.get("lat") is not None]
                if not acs:
                    continue
                ref = (d.get("now") or time.time() * 1000) / 1000
                for ac in acs:
                    age = ac.get("seen_pos", ac.get("seen", 0)) or 0
                    alt = ac.get("alt_baro")
                    on_ground = alt == "ground"
                    cand = {
                        "source": name,
                        "lat": ac["lat"],
                        "lon": ac["lon"],
                        "alt_ft": 0 if on_ground else (alt if isinstance(alt, (int, float)) else ac.get("alt_geom")),
                        "on_ground": on_ground,
                        "gs_kt": ac.get("gs"),
                        "track": ac.get("track"),
                        "vs_fpm": ac.get("baro_rate", ac.get("geom_rate")),
                        "hex": (ac.get("hex") or "").lstrip("~") or None,
                        "reg": ac.get("r"),
                        "type": ac.get("t"),
                        "callsign": (ac.get("flight") or "").strip() or None,
                        "squawk": ac.get("squawk"),
                        "at": iso(datetime.fromtimestamp(ref - age, timezone.utc)),
                        "age_s": round(time.time() - (ref - age)),
                    }
                    if best is None or cand["age_s"] < best["age_s"]:
                        best = cand
                break
            mark(state, name)
        except Exception as e:
            mark(state, name, e)
    return best


def fetch_fr24(state):
    """Horaires estimés/réels, porte, tapis bagages et immatriculation via Flightradar24."""
    name = "flightradar24"
    try:
        d = get_json(
            "https://api.flightradar24.com/common/v1/flight/list.json"
            "?query=af902&fetchBy=flight&page=1&limit=25",
            headers={"Origin": "https://www.flightradar24.com", "Referer": "https://www.flightradar24.com/"},
        )
        rows = ((d.get("result") or {}).get("response") or {}).get("data") or []
        target = parse_iso(SCHED_DEP).timestamp()

        def gap(r):
            dep = (((r.get("time") or {}).get("scheduled") or {}).get("departure")) or 0
            return abs(dep - target)

        rows = [r for r in rows if gap(r) < 12 * 3600]
        if not rows:
            mark(state, name, "vol du jour introuvable")
            return None
        row = min(rows, key=gap)
        t = row.get("time") or {}
        st = row.get("status") or {}
        gen = (st.get("generic") or {}).get("status") or {}
        dest = (row.get("airport") or {}).get("destination") or {}
        info = dest.get("info") or {}
        ac = row.get("aircraft") or {}
        ident = row.get("identification") or {}
        out = {
            "live": st.get("live"),
            "text": st.get("text"),
            "generic": gen.get("text"),
            "real_dep": iso(from_ts((t.get("real") or {}).get("departure"))),
            "est_dep": iso(from_ts((t.get("estimated") or {}).get("departure"))),
            "real_arr": iso(from_ts((t.get("real") or {}).get("arrival"))),
            "est_arr": iso(from_ts((t.get("estimated") or {}).get("arrival"))),
            "eta": iso(from_ts((t.get("other") or {}).get("eta"))),
            "terminal": info.get("terminal"),
            "gate": info.get("gate"),
            "baggage": info.get("baggage"),
            "reg": ac.get("registration"),
            "hex": (ac.get("hex") or "").lower() or None,
            "model": (ac.get("model") or {}).get("text"),
            "callsign": ident.get("callsign"),
            "id": ident.get("id"),
        }
        mark(state, name)
        return out
    except Exception as e:
        mark(state, name, e)
        return None


def fetch_fr24_position(state, fr24_id):
    """Dernier point de trace Flightradar24 (couverture satellite), si l'ADS-B ouvert ne voit rien."""
    name = "flightradar24-trace"
    try:
        d = get_json(
            f"https://data-live.flightradar24.com/clickhandler/?version=1.5&flight={fr24_id}",
            headers={"Origin": "https://www.flightradar24.com", "Referer": "https://www.flightradar24.com/"},
        )
        trail = d.get("trail") or []
        mark(state, name)
        if not trail:
            return None
        p = trail[0]
        return {
            "source": "flightradar24",
            "lat": p.get("lat"),
            "lon": p.get("lng"),
            "alt_ft": p.get("alt"),
            "on_ground": False,
            "gs_kt": p.get("spd"),
            "track": p.get("hd"),
            "vs_fpm": None,
            "at": iso(from_ts(p.get("ts"))),
            "age_s": round(time.time() - (p.get("ts") or time.time())),
        }
    except Exception as e:
        mark(state, name, e)
        return None


def fetch_flightaware(state):
    """Heures estimées d'atterrissage (piste) et d'arrivée au contact (porte) via FlightAware."""
    name = "flightaware"
    try:
        html = http("https://www.flightaware.com/live/flight/AFR902", headers={"Accept": "text/html"}).decode(
            "utf-8", "replace"
        )
        m = re.search(r"var trackpollBootstrap = (\{.*?\});\s*</script>", html, re.S)
        if not m:
            mark(state, name, "données introuvables dans la page")
            return None
        data = json.loads(m.group(1))
        target = parse_iso(SCHED_DEP).timestamp()
        best = None
        for f in (data.get("flights") or {}).values():
            cands = [f] + list((f.get("activityLog") or {}).get("flights") or [])
            for c in cands:
                if ((c.get("origin") or {}).get("iata") or "") not in ("NSI", ""):
                    continue
                dep = ((c.get("takeoffTimes") or {}).get("scheduled")) or (
                    (c.get("gateDepartureTimes") or {}).get("scheduled")
                ) or 0
                g = abs(dep - target)
                if g < 12 * 3600 and (best is None or g < best[0]):
                    best = (g, c)
        if not best:
            mark(state, name, "vol du jour introuvable")
            return None
        c = best[1]
        land = c.get("landingTimes") or {}
        gate = c.get("gateArrivalTimes") or {}
        takeoff = c.get("takeoffTimes") or {}
        dest = c.get("destination") or {}
        out = {
            "status": c.get("flightStatus"),
            "takeoff_actual": iso(from_ts(takeoff.get("actual"))),
            "landing_estimated": iso(from_ts(land.get("estimated"))),
            "landing_actual": iso(from_ts(land.get("actual"))),
            "gate_estimated": iso(from_ts(gate.get("estimated"))),
            "gate_actual": iso(from_ts(gate.get("actual"))),
            "terminal": dest.get("terminal"),
            "gate": dest.get("gate"),
        }
        mark(state, name)
        return out
    except Exception as e:
        mark(state, name, e)
        return None


# --- état ------------------------------------------------------------------

def new_state():
    return {
        "flight": FLIGHT,
        "route": "Yaoundé (NSI) → Paris-CDG",
        "updated_at": None,
        "phase": "airborne",
        "phase_label": PHASE_LABELS["airborne"],
        "landed": False,
        "landed_at": None,
        "landed_source": None,
        "at_gate": False,
        "at_gate_at": None,
        "eta": {
            "best": ANNOUNCED_ARR,
            "best_source": "Paris Aéroport (annonce 01:53)",
            "flightradar24": None,
            "flightaware_landing": None,
            "flightaware_gate": None,
            "adsb_calc": None,
            "announced": ANNOUNCED_ARR,
            "scheduled": SCHED_ARR,
        },
        "departure": {"scheduled": SCHED_DEP, "announced": ANNOUNCED_DEP, "actual": None, "source": None},
        "position": None,
        "approach_seen": None,
        "aircraft": {"reg": None, "type": "B789", "model": "Boeing 787-9", "hex": None, "callsign": None},
        "airport": {"terminal": "2E", "gate": None, "baggage": None},
        "fr24_text": None,
        "sources": {},
        "events": [],
        "notified": {},
        "run": {},
    }


def load_state():
    path = os.path.join(LIVE_DIR, "status.json")
    state = new_state()
    try:
        with open(path) as f:
            old = json.load(f)
        for k in ("landed", "landed_at", "landed_source", "at_gate", "at_gate_at", "events", "notified",
                  "aircraft", "departure", "approach_seen"):
            if k in old:
                state[k] = old[k]
        print("État précédent repris :", {k: state[k] for k in ("landed", "notified")})
    except FileNotFoundError:
        pass
    except Exception:
        traceback.print_exc()
    return state


def add_event(state, kind, text):
    state["events"].append({"at": iso(now()), "type": kind, "text": text})
    print(f"[événement] {kind}: {text}", flush=True)


def adsb_eta(pos):
    """Estimation maison : vitesse sol jusqu'à 200 km de CDG, puis ~28 min de descente/approche."""
    gs = (pos.get("gs_kt") or 0) * 1.852
    d = pos["dist_km"]
    if pos.get("on_ground") or gs < 150:
        return None
    minutes = (d - 200) / gs * 60 + 28 if d > 200 else d / 200 * 28
    return parse_iso(pos["at"]) + timedelta(minutes=minutes)


def update(state, adsb, fr24, fa):
    t = now()
    state["updated_at"] = iso(t)
    ac = state["aircraft"]
    ap = state["airport"]

    if fr24:
        state["fr24_text"] = fr24.get("text")
        for k_src, k_dst in (("reg", "reg"), ("hex", "hex"), ("callsign", "callsign"), ("model", "model")):
            if fr24.get(k_src):
                ac[k_dst] = fr24[k_src]
        for k in ("terminal", "gate", "baggage"):
            if fr24.get(k):
                ap[k] = fr24[k]
        if fr24.get("real_dep"):
            state["departure"].update(actual=fr24["real_dep"], source="Flightradar24")
        state["eta"]["flightradar24"] = fr24.get("est_arr") or fr24.get("eta")
    if fa:
        if fa.get("terminal"):
            ap["terminal"] = fa["terminal"]
        if fa.get("gate") and not ap.get("gate"):
            ap["gate"] = fa["gate"]
        if fa.get("takeoff_actual") and not state["departure"].get("actual"):
            state["departure"].update(actual=fa["takeoff_actual"], source="FlightAware")
        state["eta"]["flightaware_landing"] = fa.get("landing_estimated")
        state["eta"]["flightaware_gate"] = fa.get("gate_estimated")

    pos = adsb
    if pos:
        pos["dist_km"] = round(haversine_km((pos["lat"], pos["lon"]), CDG))
        for k in ("hex", "reg", "type", "callsign"):
            if pos.get(k) and not ac.get(k):
                ac[k] = pos[k]
        eta = adsb_eta(pos)
        state["eta"]["adsb_calc"] = iso(eta)
        # Mémorise un passage en courte finale (sert à confirmer l'atterrissage).
        if not pos["on_ground"] and (pos["alt_ft"] or 99999) < 2500 and pos["dist_km"] < 25:
            state["approach_seen"] = pos["at"]
        state["position"] = pos

    # --- atterrissage -----------------------------------------------------
    if not state["landed"]:
        landed = None
        if fr24 and (fr24.get("real_arr") or fr24.get("generic") == "landed"):
            landed = (fr24.get("real_arr") or iso(t), "Flightradar24")
        elif fa and fa.get("landing_actual"):
            landed = (fa["landing_actual"], "FlightAware")
        elif pos and pos["on_ground"] and pos["dist_km"] < 10:
            landed = (pos["at"], f"ADS-B ({pos['source']}) : au sol à CDG")
        elif (
            state.get("approach_seen")
            and (pos is None or pos["age_s"] > 240)
            and t - parse_iso(state["approach_seen"]) < timedelta(minutes=20)
            and t - parse_iso(state["approach_seen"]) > timedelta(minutes=4)
        ):
            landed = (state["approach_seen"], "ADS-B : signal perdu en courte finale")
        if landed:
            state["landed"] = True
            state["landed_at"], state["landed_source"] = landed
            add_event(state, "landed", f"Atterri à {hhmm(parse_iso(landed[0]))} (source : {landed[1]})")

    if not state["at_gate"] and fa and fa.get("gate_actual"):
        state["at_gate"] = True
        state["at_gate_at"] = fa["gate_actual"]
        add_event(state, "at_gate", f"Arrivé au contact à {hhmm(parse_iso(fa['gate_actual']))}")

    # --- phase ------------------------------------------------------------
    if state["at_gate"]:
        phase = "at_gate"
    elif state["landed"]:
        phase = "landed"
    elif pos and pos["age_s"] < 900:
        alt = pos["alt_ft"] or 0
        vs = pos.get("vs_fpm") or 0
        if pos["dist_km"] < 60 and alt < 8000:
            phase = "approach"
        elif pos["dist_km"] < 450 and (vs < -600 or alt < 28000):
            phase = "descent"
        else:
            phase = "cruise"
    elif state["departure"].get("actual") or t > parse_iso(ANNOUNCED_DEP):
        phase = "airborne"
    else:
        phase = "scheduled"
    if phase != state["phase"]:
        if phase == "descent" and "descent" not in [e["type"] for e in state["events"]]:
            add_event(state, "descent", "Début de la descente vers Paris")
        if phase == "approach" and "approach" not in [e["type"] for e in state["events"]]:
            add_event(state, "approach", "En approche finale sur CDG")
    state["phase"] = phase
    state["phase_label"] = PHASE_LABELS[phase]

    # --- meilleure estimation --------------------------------------------
    e = state["eta"]
    if state["landed"]:
        e["best"], e["best_source"] = state["landed_at"], "atterrissage réel"
    else:
        for key, label in (
            ("flightradar24", "Flightradar24"),
            ("flightaware_landing", "FlightAware"),
            ("adsb_calc", "calcul ADS-B"),
            ("announced", "Paris Aéroport (annonce 01:53)"),
        ):
            v = e.get(key)
            if v and parse_iso(v) > t - timedelta(minutes=30):
                e["best"], e["best_source"] = v, label
                break


# --- notifications ---------------------------------------------------------

def notify(state):
    sent = state["notified"]
    eta = parse_iso(state["eta"]["best"])
    ap = state["airport"]
    if state["landed"] and not sent.get("landed"):
        t = hhmm(parse_iso(state["landed_at"]))
        title = f"AF902 a atterri à CDG ({t})"
        lines = [
            f"Atterrissage à {t} (heure de Paris), source : {state['landed_source']}.",
            f"Terminal {ap.get('terminal') or '2E'}"
            + (f", porte {ap['gate']}" if ap.get("gate") else "")
            + (f", tapis bagages {ap['baggage']}" if ap.get("baggage") else "")
            + ".",
            "Compter en général 30 à 60 min avant la sortie (roulage, passeports, bagages).",
        ]
        ntfy(title, "\n".join(lines), priority=5, tags=["airplane_arriving", "tada"])
        gh_issue(title, "\n\n".join(lines + [MENTION]).strip())
        sent["landed"] = iso(now())
    elif state["phase"] == "descent" and not sent.get("descent") and not state["landed"]:
        ntfy("AF902 commence sa descente", f"Atterrissage estimé vers {hhmm(eta)} ({state['eta']['best_source']}).",
             priority=4, tags=["airplane"])
        sent["descent"] = iso(now())
    if state["at_gate"] and not sent.get("at_gate"):
        ntfy("AF902 est au terminal", f"Avion au contact à {hhmm(parse_iso(state['at_gate_at']))}.",
             priority=3, tags=["door"])
        sent["at_gate"] = iso(now())


def ntfy(title, message, priority=4, tags=None):
    if not NTFY_TOPIC:
        return
    try:
        body = {"topic": NTFY_TOPIC, "title": title, "message": message, "priority": priority, "tags": tags or []}
        http("https://ntfy.sh/", data=json.dumps(body).encode(), method="POST",
             headers={"Content-Type": "application/json"})
        print("ntfy envoyé :", title, flush=True)
    except Exception as e:
        print("ntfy en échec :", e, flush=True)


def gh_issue(title, body):
    if not (GH_TOKEN and GH_REPO):
        return
    try:
        http(
            f"https://api.github.com/repos/{GH_REPO}/issues",
            data=json.dumps({"title": title, "body": body}).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {GH_TOKEN}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
        )
        print("Issue GitHub créée :", title, flush=True)
    except Exception as e:
        print("Issue GitHub en échec :", e, flush=True)


# --- écriture --------------------------------------------------------------

def render_md(state):
    p = state["position"]
    e = state["eta"]
    ap = state["airport"]
    ac = state["aircraft"]
    upd = parse_iso(state["updated_at"])
    out = [
        f"# AF902 Yaoundé → Paris-CDG : {state['phase_label']}",
        "",
        f"_Mis à jour à {upd.astimezone(PARIS).strftime('%H:%M:%S')} (heure de Paris). "
        "Rafraîchir la page pour la dernière version._",
        "",
    ]
    if state["landed"]:
        out.append(f"## Atterri à {hhmm(parse_iso(state['landed_at']))}  ({state['landed_source']})")
    else:
        out.append(f"## Atterrissage estimé : {hhmm(parse_iso(e['best']))}  ({e['best_source']})")
    out += ["", "| | |", "|---|---|"]
    if p:
        out.append(f"| Position | {p['lat']:.2f}, {p['lon']:.2f} · **{p['dist_km']} km de CDG** "
                   f"(il y a {p['age_s']} s, {p['source']}) |")
        alt = "au sol" if p["on_ground"] else f"{p['alt_ft']:,} ft".replace(",", " ") if p["alt_ft"] else "—"
        vs = f" ({p['vs_fpm']:+} ft/min)" if p.get("vs_fpm") else ""
        out.append(f"| Altitude | {alt}{vs} |")
        if p.get("gs_kt"):
            out.append(f"| Vitesse sol | {round(p['gs_kt'])} kt ({round(p['gs_kt'] * 1.852)} km/h) |")
    else:
        out.append("| Position | pas de position ADS-B (zone sans récepteurs, normal au-dessus du Sahara) |")
    out.append(f"| Avion | {ac.get('model') or 'Boeing 787-9'} {ac.get('reg') or ''} |")
    out.append(f"| Arrivée | Terminal {ap.get('terminal') or '2E'} · porte {ap.get('gate') or '—'} "
               f"· tapis bagages {ap.get('baggage') or '—'} |")
    dep = state["departure"]
    out.append(f"| Décollage | {hhmm(parse_iso(dep.get('actual')))} heure de Paris "
               f"({dep.get('source') or 'non confirmé'}) |")
    out += ["", "## Estimations d'arrivée (heure de Paris)", "", "| Source | Heure |", "|---|---|"]
    for key, label in (
        ("flightradar24", "Flightradar24 (atterrissage)"),
        ("flightaware_landing", "FlightAware (atterrissage)"),
        ("flightaware_gate", "FlightAware (arrivée porte)"),
        ("adsb_calc", "Calcul ADS-B (distance / vitesse)"),
        ("announced", "Paris Aéroport, annonce de 01:53"),
        ("scheduled", "Horaire initial"),
    ):
        out.append(f"| {label} | {hhmm(parse_iso(e.get(key)))} |")
    if state["events"]:
        out += ["", "## Journal", ""]
        out += [f"- {hhmm(parse_iso(ev['at']))} · {ev['text']}" for ev in state["events"]]
    out += ["", "## Sources", "", "| Source | État |", "|---|---|"]
    for name, s in sorted(state["sources"].items()):
        out.append(f"| {name} | {'ok' if s['ok'] else 'erreur : ' + (s['error'] or '')} |")
    return "\n".join(out) + "\n"


def write_files(state):
    os.makedirs(LIVE_DIR, exist_ok=True)
    with open(os.path.join(LIVE_DIR, "status.json"), "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    with open(os.path.join(LIVE_DIR, "STATUS.md"), "w") as f:
        f.write(render_md(state))
    p = state["position"] or {}
    point = {
        "t": state["updated_at"],
        "phase": state["phase"],
        "eta": state["eta"]["best"],
        "eta_src": state["eta"]["best_source"],
        "lat": p.get("lat"),
        "lon": p.get("lon"),
        "alt": p.get("alt_ft"),
        "gs": p.get("gs_kt"),
        "dist": p.get("dist_km"),
        "age": p.get("age_s"),
    }
    with open(os.path.join(LIVE_DIR, "history.jsonl"), "a") as f:
        f.write(json.dumps(point, ensure_ascii=False) + "\n")


def git(*args):
    return subprocess.run(["git", "-C", LIVE_DIR, *args], capture_output=True, text=True)


def commit_push(state):
    git("add", "-A")
    msg = f"{hhmm(now())} {state['phase_label']} · ETA {hhmm(parse_iso(state['eta']['best']))}"
    r = git("commit", "-q", "-m", msg)
    if r.returncode != 0 and "nothing to commit" in (r.stdout + r.stderr):
        return
    for i in range(4):
        # Un seul écrivain (concurrency du workflow) : la dernière exécution fait foi.
        r = git("push", "-q", "--force", "origin", "HEAD:refs/heads/live")
        if r.returncode == 0:
            return
        time.sleep(2 ** (i + 1))
    print("push en échec :", r.stderr, flush=True)


# --- boucle principale -------------------------------------------------------

def main():
    state = load_state()
    state["run"] = {
        "id": os.environ.get("GITHUB_RUN_ID"),
        "started_at": iso(now()),
        "url": f"https://github.com/{GH_REPO}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}" if GH_REPO else None,
    }
    started = time.time()
    last_commit = 0.0
    fr24 = fa = None
    tick = 0
    while True:
        tick += 1
        t0 = time.time()
        try:
            adsb = fetch_adsb(state)
            if tick % 2 == 1 or state["phase"] in ("approach", "descent"):
                fr24 = fetch_fr24(state) or fr24
            if adsb is None and fr24 and fr24.get("id") and fr24.get("live"):
                adsb = fetch_fr24_position(state, fr24["id"])
            if tick % 4 == 1 or state["phase"] in ("approach", "landed"):
                fa = fetch_flightaware(state) or fa
            update(state, adsb, fr24, fa)
            notify(state)
            write_files(state)
            eta = parse_iso(state["eta"]["best"])
            close = eta and abs((eta - now()).total_seconds()) < 3600
            if close or state["landed"] or time.time() - last_commit > 170 or tick == 1:
                commit_push(state)
                last_commit = time.time()
            p = state["position"] or {}
            print(
                f"{hhmm(now())} {state['phase']:<9} ETA {hhmm(eta)} ({state['eta']['best_source']}) "
                f"dist={p.get('dist_km')} alt={p.get('alt_ft')} src={p.get('source')} "
                f"ok={[k for k, v in state['sources'].items() if v['ok']]}",
                flush=True,
            )
        except Exception:
            traceback.print_exc()
        elapsed_min = (time.time() - started) / 60
        done_after_landing = state["landed"] and (
            state["at_gate"] or now() - parse_iso(state["landed_at"]) > timedelta(minutes=45)
        )
        if done_after_landing or elapsed_min > MAX_MIN:
            commit_push(state)
            print("Fin du suivi.", flush=True)
            return
        time.sleep(max(5, POLL_S - (time.time() - t0)))


if __name__ == "__main__":
    sys.exit(main())
