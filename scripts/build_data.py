#!/usr/bin/env python3
"""
Collecte les données du Datacenter IBU (biathlonresults.com) et les écrit en
fichiers JSON statiques pour le site GitHub Pages.

Usage :
    python3 scripts/build_data.py --out site/data [--seasons-back 10]

Le dossier de sortie sert aussi de cache : un fichier déjà présent et définitif
(saison terminée, course officielle) n'est jamais retéléchargé. Seules les
données de la saison en cours sont rafraîchies à chaque passage.

Aucune dépendance : bibliothèque standard Python 3.9+.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("IBU_API_BASE", "https://biathlonresults.com/modules/sportapi/api/")
UA = "biathlon-datacenter-pages/1.0 (+https://github.com; collecte planifiee, usage non commercial)"
WORKERS = int(os.environ.get("IBU_WORKERS", "4"))
INDIVIDUAL = {"SP", "PU", "IN", "MS", "SI"}

_lock = threading.Lock()
stats = {"requests": 0, "errors": 0}


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    if not s:
        return None
    try:
        return dt.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def get(method, **params):
    """Appel GET à l'API IBU, avec 3 tentatives. Retourne le JSON ou lève une exception."""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = API + method + ("?" + qs if qs else "")
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read().decode("utf-8-sig")
            with _lock:
                stats["requests"] += 1
            return json.loads(body) if body.strip() else None
        except (urllib.error.URLError, TimeoutError, ValueError, ConnectionError) as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code in (400, 404):
                break
            time.sleep(1.5 * (attempt + 1) + random.random())
    with _lock:
        stats["errors"] += 1
    raise RuntimeError(f"{method} {params}: {last}")


def slim(obj, keys):
    return {k: obj.get(k) for k in keys if obj.get(k) not in (None, "", [])}


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def pmap(fn, items):
    """Exécute fn sur chaque élément en parallèle (WORKERS) ; les erreurs sont journalisées."""
    out = {}
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for f in cf.as_completed(futs):
            it = futs[f]
            try:
                out[it] = f.result()
            except Exception as e:  # noqa: BLE001
                print(f"  ! {it}: {e}", file=sys.stderr)
                out[it] = None
    return out


# --------------------------------------------------------------------------- champs conservés
EVENT_KEYS = ["EventId", "ShortDescription", "Description", "Organizer", "Nat", "NatLong",
              "StartDate", "EndDate", "EventClassificationId", "Level"]
RACE_KEYS = ["RaceId", "ShortDescription", "Description", "StartTime", "DisciplineId", "catId", "km",
             "ResultStatus", "StatusText", "ScheduleStatus", "NrShootings", "ShootingPositions",
             "NrLegs", "HasAnalysis", "Location"]
RESULT_KEYS = ["Rank", "IRM", "Bib", "IBUId", "Name", "ShortName", "Nat", "Shootings", "ShootingTotal",
               "TotalTime", "Behind", "Result", "IsTeam", "Leg", "WC", "NC", "StartInfo", "PursuitStartDistance"]
CUP_ROW_KEYS = ["Rank", "IBUId", "Name", "ShortName", "Nat", "Score"]
ALLRES_KEYS = ["RaceId", "Comp", "Competition", "Level", "Place", "PlaceNat", "Rank", "Season", "Shootings", "Pen"]
BIO_DROP = {"Recent", "TopResults", "FlagURI", "Stories", "CompetitionRankings"}


def is_final(race):
    return str(race.get("ResultStatus") or "").upper() in ("OFFICIAL", "UNOFFICIAL") and \
        str(race.get("ScheduleStatus") or "").upper() in ("FINISHED", "")


# --------------------------------------------------------------------------- collecte
def build(out, seasons_back, athlete_max_age_days, max_athletes):
    t0 = time.time()
    state_path = os.path.join(out, "_state.json")
    state = read_json(state_path, {}) or {}
    state.setdefault("races", {})      # RaceId -> {"final": bool, "at": iso}
    state.setdefault("athletes", {})   # IBUId -> iso de la dernière collecte
    state.setdefault("seasons_done", [])

    # 1. Saisons
    seasons = get("Seasons") or []
    by_id = {s["SeasonId"]: s for s in seasons if s.get("SeasonId")}
    cur = next((s["SeasonId"] for s in seasons if s.get("IsCurrent")), None)
    cur_res = next((s["SeasonId"] for s in seasons if s.get("IsCurrentResults")), None) or cur
    ordered = sorted(by_id.values(), key=lambda s: s.get("SortOrder") or 0, reverse=True)
    top = by_id.get(cur) or by_id.get(cur_res)
    if top:  # on ignore les saisons futures éventuellement listées
        ordered = [s for s in ordered if (s.get("SortOrder") or 0) <= (top.get("SortOrder") or 0)]
    extra = 1 if cur and cur_res and cur != cur_res else 0
    covered = [s["SeasonId"] for s in ordered[: seasons_back + extra]]
    live = {x for x in (cur, cur_res) if x}
    print(f"Saisons couvertes : {', '.join(covered)} (en cours : {cur}, résultats : {cur_res})")

    touched_athletes = set()
    all_athletes = {}  # IBUId -> [Name, Nat, g]

    def note_athlete(ibu, name, nat, g):
        if not ibu or len(ibu) < 10:  # les équipes ont un IBUId court (BTFRA2)
            return
        a = all_athletes.get(ibu)
        if not a:
            all_athletes[ibu] = [name or "", nat or "", g or ""]
        else:
            if not a[2] and g:
                a[2] = g

    for sid in covered:
        sfile = os.path.join(out, "seasons", f"{sid}.json")
        if sid not in live and sid in state["seasons_done"] and os.path.exists(sfile):
            season = read_json(sfile)
            for ibu, st in (season.get("starters") or {}).items():
                note_athlete(ibu, st.get("n"), st.get("nat"), st.get("g"))
            for cup in season.get("cups", []):
                rows = (read_json(os.path.join(out, "cups", cup["CupId"] + ".json"), {}) or {}).get("Rows", [])
                for r in rows:
                    note_athlete(r.get("IBUId"), r.get("Name"), r.get("Nat"), cup["CupId"][-4:-2])
            print(f"[{sid}] déjà complète, conservée")
            continue

        print(f"[{sid}] collecte…")
        events = [slim(e, EVENT_KEYS) for e in (get("Events", SeasonId=sid, Level=1) or [])
                  if str(e.get("EventId", "")).startswith("BT")]
        comps = pmap(lambda eid: get("Competitions", EventId=eid) or [], [e["EventId"] for e in events])
        for e in events:
            e["races"] = sorted([slim(c, RACE_KEYS) for c in (comps.get(e["EventId"]) or [])],
                                key=lambda c: c.get("StartTime") or "")
        events.sort(key=lambda e: e.get("StartDate") or "")

        cups = [slim(c, ["CupId", "Description", "ShortDescription"]) for c in (get("Cups", SeasonId=sid) or [])
                if str(c.get("CupId", "")).startswith("BT") and str(c.get("Level")) == "1"]
        cup_data = pmap(lambda cid: get("CupResults", CupId=cid), [c["CupId"] for c in cups])
        kept_cups = []
        for c in cups:
            d = cup_data.get(c["CupId"])
            path = os.path.join(out, "cups", c["CupId"] + ".json")
            if d is None:
                if os.path.exists(path):
                    kept_cups.append(c)
                continue
            rows = [slim(r, CUP_ROW_KEYS) for r in (d.get("Rows") or [])]
            write_json(path, {"CupId": c["CupId"], "CupName": d.get("CupName"), "AsOf": d.get("AsOf"),
                              "RaceCount": d.get("RaceCount"), "TotalRaces": d.get("TotalRaces"), "Rows": rows})
            kept_cups.append(c)
            for r in rows:
                note_athlete(r.get("IBUId"), r.get("Name"), r.get("Nat"), c["CupId"][-4:-2])

        # Courses : on ne retélécharge que ce qui n'est pas définitif.
        horizon = now_utc() + dt.timedelta(days=2)
        todo = []
        for e in events:
            for r in e["races"]:
                rid = r["RaceId"]
                st = state["races"].get(rid)
                exists = os.path.exists(os.path.join(out, "races", rid + ".json"))
                start = parse_time(r.get("StartTime"))
                if start and start > horizon:
                    continue  # course lointaine : rien à télécharger
                if exists and st and st.get("final"):
                    continue
                todo.append(r)

        def fetch_race(rid):
            meta = next(r for r in todo if r["RaceId"] == rid)
            d = get("Results", RaceId=rid) or {}
            rows = [slim(x, RESULT_KEYS) for x in (d.get("Results") or [])]
            comp = slim(d.get("Competition") or meta, RACE_KEYS)
            final = is_final(comp) and bool(rows) and not d.get("IsStartList")
            ski = {}
            if final and rid[-2:] in INDIVIDUAL and comp.get("HasAnalysis") is not False:
                try:
                    an = get("AnalyticResults", RaceId=rid, TypeId="CRST") or {}
                    for x in an.get("Results") or []:
                        if x.get("IBUId") and str(x.get("Rank") or "").isdigit():
                            ski[x["IBUId"]] = int(x["Rank"])
                except RuntimeError as e:
                    print(f"  ! analyse {rid}: {e}", file=sys.stderr)
            evt = slim(d.get("SportEvt") or {}, EVENT_KEYS)
            write_json(os.path.join(out, "races", rid + ".json"), {
                "RaceId": rid, "IsStartList": bool(d.get("IsStartList")), "Competition": comp,
                "SportEvt": evt, "Results": rows, "Ski": ski, "fetchedAt": iso(now_utc())})
            return final

        res = pmap(fetch_race, [r["RaceId"] for r in todo])
        for rid, final in res.items():
            if final is not None:
                state["races"][rid] = {"final": bool(final), "at": iso(now_utc())}
        print(f"  {len(events)} étapes, {len(kept_cups)} classements, {len(todo)} courses mises à jour")

        # Partants de la saison (courses individuelles) pour les pages Nations
        starters = {}
        for e in events:
            for r in e["races"]:
                rid = r["RaceId"]
                if rid[-2:] not in INDIVIDUAL:
                    continue
                rd = read_json(os.path.join(out, "races", rid + ".json"))
                if not rd or rd.get("IsStartList"):
                    continue
                g = rid[-4:-2]
                for x in rd.get("Results") or []:
                    ibu = x.get("IBUId")
                    if not ibu or x.get("IsTeam"):
                        continue
                    s = starters.setdefault(ibu, {"n": x.get("Name"), "nat": x.get("Nat"), "g": g, "s": 0})
                    s["s"] += 1
                    rk = x.get("Rank")
                    if rk and str(rk).isdigit():
                        s["b"] = min(int(rk), s.get("b", 999))
                    note_athlete(ibu, x.get("Name"), x.get("Nat"), g)
                    if rid in res:
                        touched_athletes.add(ibu)
        write_json(sfile, {"SeasonId": sid, "Description": by_id.get(sid, {}).get("Description"),
                           "events": events, "cups": kept_cups, "starters": starters,
                           "fetchedAt": iso(now_utc())})
        if sid not in live and sid not in state["seasons_done"]:
            pending = [r for e in events for r in e["races"] if not state["races"].get(r["RaceId"], {}).get("final")]
            if not pending:
                state["seasons_done"].append(sid)
        write_json(state_path, state)

    # 2. Fiches athlètes
    current_ids = set()
    for sid in live:
        season = read_json(os.path.join(out, "seasons", f"{sid}.json"), {}) or {}
        current_ids |= set((season.get("starters") or {}).keys())
    max_age = dt.timedelta(days=athlete_max_age_days)
    todo = []
    for ibu in all_athletes:
        path = os.path.join(out, "athletes", ibu + ".json")
        last = parse_time(state["athletes"].get(ibu))
        if not os.path.exists(path) or ibu in touched_athletes:
            todo.append(ibu)
        elif ibu in current_ids and (not last or now_utc() - last > max_age):
            todo.append(ibu)
    todo.sort(key=lambda i: (os.path.exists(os.path.join(out, "athletes", i + ".json")), i))
    if max_athletes and len(todo) > max_athletes:
        print(f"Fiches athlètes : {len(todo)} à jour, limitées à {max_athletes} pour ce passage")
        todo = todo[:max_athletes]
    print(f"Fiches athlètes : {len(todo)} à télécharger sur {len(all_athletes)}")

    def fetch_athlete(ibu):
        bio = get("CISBios", IBUId=ibu) or {}
        allr = get("AllResults", IBUId=ibu) or {}
        bio = {k: v for k, v in bio.items() if k not in BIO_DROP and v not in (None, "", [])}
        results = [slim(r, ALLRES_KEYS) for r in (allr.get("Results") or [])]
        write_json(os.path.join(out, "athletes", ibu + ".json"),
                   {"IBUId": ibu, "bio": bio, "results": results, "fetchedAt": iso(now_utc())})
        return bio

    done = pmap(fetch_athlete, todo)
    for ibu, bio in done.items():
        if bio is None:
            continue
        state["athletes"][ibu] = iso(now_utc())
        a = all_athletes.get(ibu)
        if a is not None:
            full = bio.get("FullName")
            fam, giv = bio.get("FamilyName"), bio.get("GivenName")
            if fam:
                a[0] = f"{fam} {giv or ''}".strip()
            elif full:
                a[0] = full
            if bio.get("GenderId") in ("W", "M"):
                a[2] = "SW" if bio["GenderId"] == "W" else "SM"
    write_json(state_path, state)

    # 3. Index et méta
    index = sorted(([ibu, *v] for ibu, v in all_athletes.items()), key=lambda r: r[1])
    write_json(os.path.join(out, "athletes.json"), index)
    meta = {
        "generatedAt": iso(now_utc()),
        "currentSeason": cur,
        "currentResultsSeason": cur_res,
        "seasons": [{"SeasonId": s, "Description": by_id.get(s, {}).get("Description")} for s in covered
                    if os.path.exists(os.path.join(out, "seasons", f"{s}.json"))],
        "athletes": len(index),
        "requests": stats["requests"],
        "errors": stats["errors"],
        "durationSec": round(time.time() - t0),
    }
    write_json(os.path.join(out, "meta.json"), meta)
    print(json.dumps(meta, ensure_ascii=False))
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--seasons-back", type=int, default=int(os.environ.get("SEASONS_BACK", "10")))
    ap.add_argument("--athlete-max-age-days", type=float, default=2)
    ap.add_argument("--max-athletes", type=int, default=int(os.environ.get("MAX_ATHLETES", "0")))
    args = ap.parse_args()
    meta = build(args.out, args.seasons_back, args.athlete_max_age_days, args.max_athletes)
    # Échec si l'API n'a rien renvoyé du tout : le site précédent reste en ligne.
    if meta["requests"] == 0 or (meta["errors"] > 50 and meta["errors"] > meta["requests"]):
        sys.exit("Trop d'erreurs pendant la collecte.")


if __name__ == "__main__":
    main()
