"""
World Cup 2026 sweepstake - standalone mini-app (Supabase + live results).

Friends open the page and add their own names. The organiser (with a PIN) runs
the draw: the strongest N teams are handed out one each (N = number of players),
the rest sit it out. Once the tournament starts, the page shows who's still in,
who's out, and a leaderboard by how far each team has gone.

State is stored in Supabase (Postgres). Live scores come from API-Football and
are cached server-side so we stay inside the free 100-requests-a-day limit.

Environment variables (set these where you deploy):
  DATABASE_URL       Supabase connection string (Session pooler, URI form)
  API_FOOTBALL_KEY   your API-Football key (optional - no key = no live scores)
  ORGANISER_PIN      the PIN that unlocks Draw/Reset (default: 1966)
"""
import json
import os
import random
import re
import time
import unicodedata
from pathlib import Path

import httpx
import psycopg
from psycopg.types.json import Json
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE = Path(__file__).parent
TEAMS = json.loads((BASE / "teams.json").read_text(encoding="utf-8"))

DATABASE_URL = os.environ.get("DATABASE_URL", "")
API_KEY = os.environ.get("API_FOOTBALL_KEY", "")
ORGANISER_PIN = os.environ.get("ORGANISER_PIN", "1966")
MAX_NAME = 40

API_BASE = "https://v3.football.api-sports.io"
LEAGUE_ID = 1          # FIFA World Cup
SEASON = 2026
LIVE_TTL = 900         # seconds between live-score refreshes (15 min -> well under 100/day)

app = FastAPI(title="World Cup 2026 Sweepstake")


def strength(rank: int) -> int:
    return 49 - rank


# ----------------------------------------------------------------------------
# Name matching: API-Football names -> our team names
# ----------------------------------------------------------------------------
def normalise(name: str) -> str:
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(c for c in n if not unicodedata.combining(c)).lower().strip()
    n = re.sub(r"[^a-z0-9 ]", "", n)
    return re.sub(r"\s+", " ", n)


MY_NORM = {normalise(t["team"]): t["team"] for t in TEAMS}

# common API-Football spellings -> our normalised name
ALIASES = {
    "united states": "usa",
    "korea republic": "south korea",
    "ir iran": "iran",
    "turkey": "turkiye",
    "czech republic": "czechia",
    "cote divoire": "ivory coast",
    "cabo verde": "cape verde",
    "cape verde islands": "cape verde",
    "congo dr": "dr congo",
    "democratic republic of the congo": "dr congo",
    "bosnia herzegovina": "bosnia and herzegovina",
}


def display_for(api_name: str):
    n = normalise(api_name)
    n = ALIASES.get(n, n)
    return MY_NORM.get(n)


STAGE_LABEL = {1: "Group stage", 2: "Round of 32", 3: "Round of 16",
               4: "Quarter-finals", 5: "Semi-finals", 6: "Final", 7: "Champion"}
FINISHED = {"FT", "AET", "PEN", "WO", "AWD"}


def stage_rank(round_str: str) -> int:
    s = (round_str or "").lower()
    if "group" in s:
        return 1
    if "32" in s:
        return 2
    if "16" in s:
        return 3
    if "quarter" in s:
        return 4
    if "semi" in s:
        return 5
    if "3rd" in s or "third" in s or "place" in s:
        return 5          # third-place match = reached the semis
    if "final" in s:
        return 6
    return 1


def compute_live(fixtures):
    """Pure function: API-Football fixtures -> per-team status. Returns
    (teams_data, started). teams_data maps our team name -> dict with
    live ('in'/'out'/'champion'), stage_rank, stage label, and last result."""
    data = {}
    group_unfinished = False
    any_knockout = False
    started = False

    for fx in fixtures:
        rnd = (fx.get("league") or {}).get("round", "")
        rank = stage_rank(rnd)
        if rank >= 2:
            any_knockout = True
        teams = fx.get("teams") or {}
        goals = fx.get("goals") or {}
        st = ((fx.get("fixture") or {}).get("status") or {}).get("short", "")
        finished = st in FINISHED
        if finished:
            started = True
        date = (fx.get("fixture") or {}).get("date", "")

        sides = [
            (teams.get("home") or {}, teams.get("away") or {}, goals.get("home"), goals.get("away")),
            (teams.get("away") or {}, teams.get("home") or {}, goals.get("away"), goals.get("home")),
        ]
        for me, opp, gf, ga in sides:
            name = display_for(me.get("name", ""))
            if not name:
                continue
            info = data.setdefault(name, {"stage_rank": 1, "eliminated": False,
                                          "champion": False, "last": None, "last_date": ""})
            if rank > info["stage_rank"]:
                info["stage_rank"] = rank
            if not finished and rank == 1:
                group_unfinished = True
            if finished:
                win = me.get("winner")
                opp_name = display_for(opp.get("name", "")) or opp.get("name", "?")
                if gf is not None and ga is not None:
                    verb = "beat" if win is True else ("lost to" if win is False else "drew with")
                    text = f"{verb} {opp_name} {gf}-{ga}"
                else:
                    text = None
                if date > info["last_date"]:
                    info["last_date"] = date
                    info["last"] = text
                if rank >= 2 and win is False:
                    info["eliminated"] = True
                if rank == 6 and win is True:
                    info["champion"] = True

    groups_done = not group_unfinished
    out = {}
    for name, info in data.items():
        if info["champion"]:
            status, info["stage_rank"] = "champion", 7
        elif info["eliminated"]:
            status = "out"
        elif groups_done and any_knockout and info["stage_rank"] < 2:
            status = "out"      # groups are done and they're not in the knockouts
        else:
            status = "in"
        out[name] = {
            "live": status,
            "stage_rank": info["stage_rank"],
            "stage": STAGE_LABEL[min(info["stage_rank"], 7)],
            "last": info["last"],
        }
    return out, started


# ----------------------------------------------------------------------------
# Live results cache
# ----------------------------------------------------------------------------
_live = {"at": 0.0, "data": {}, "started": False, "error": None, "fetched": False}


def get_live():
    if not API_KEY:
        return {"data": {}, "started": False, "updated": None, "error": "no_key"}
    now = time.time()
    if _live["fetched"] and now - _live["at"] < LIVE_TTL:
        return {"data": _live["data"], "started": _live["started"],
                "updated": _live["at"], "error": _live["error"]}
    try:
        r = httpx.get(f"{API_BASE}/fixtures",
                      params={"league": LEAGUE_ID, "season": SEASON},
                      headers={"x-apisports-key": API_KEY}, timeout=12)
        payload = r.json()
        errs = payload.get("errors")
        has_err = bool(errs) and (errs if isinstance(errs, list) else list(errs.values()))
        if r.status_code != 200 or has_err:
            _live["error"] = "api_error"
            _live["at"] = now            # back off; don't hammer the API on errors
        else:
            data, started = compute_live(payload.get("response", []))
            _live.update({"data": data, "started": started, "error": None,
                          "at": now, "fetched": True})
    except Exception:
        _live["error"] = "unreachable"
        _live["at"] = now
    return {"data": _live["data"], "started": _live["started"],
            "updated": _live["at"] if _live["fetched"] else None, "error": _live["error"]}


# ----------------------------------------------------------------------------
# Database (Supabase / Postgres)
# ----------------------------------------------------------------------------
def connect():
    if not DATABASE_URL:
        raise HTTPException(500, "The database isn't configured yet (DATABASE_URL is missing).")
    return psycopg.connect(DATABASE_URL, prepare_threshold=None, connect_timeout=10)


def ensure():
    if not DATABASE_URL:
        return
    with connect() as con, con.cursor() as cur:
        cur.execute("create table if not exists players ("
                    "id bigint generated always as identity primary key, "
                    "name text not null, joined_at timestamptz not null default now())")
        cur.execute("create unique index if not exists players_name_ci on players (lower(name))")
        cur.execute("create table if not exists state ("
                    "id int primary key default 1 check (id = 1), "
                    "drawn boolean not null default false, "
                    "results jsonb not null default '[]'::jsonb)")
        cur.execute("insert into state (id) values (1) on conflict (id) do nothing")
        con.commit()


try:
    ensure()
except Exception:
    pass  # if the DB is briefly unreachable at boot, the routes will surface it


def read_players(cur):
    cur.execute("select name from players order by id")
    return [row[0] for row in cur.fetchall()]


def read_state(cur):
    cur.execute("select drawn, results from state where id = 1")
    row = cur.fetchone()
    if not row:
        return False, []
    return bool(row[0]), (row[1] or [])


def build_state():
    with connect() as con, con.cursor() as cur:
        players = read_players(cur)
        drawn, results = read_state(cur)

    live = get_live()
    started = live["started"]
    ld = live["data"]
    for row in results:
        info = ld.get(row["team"])
        if info:
            row["live"] = info["live"]
            row["stage"] = info["stage"]
            row["last"] = info["last"]

    if results and started and ld:
        def key(r):
            info = ld.get(r["team"], {})
            rank = info.get("stage_rank", 1)
            prio = {"champion": 2, "in": 1, "out": 0}.get(info.get("live"), 1)
            return (rank, prio, r["strength"])
        results = sorted(results, key=key, reverse=True)
    else:
        results = sorted(results, key=lambda r: -r["strength"])

    return {
        "players": players, "count": len(players), "drawn": drawn,
        "results": results, "started": started,
        "liveUpdated": live["updated"], "liveError": live["error"],
        "totalTeams": len(TEAMS),
    }


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
class JoinIn(BaseModel):
    name: str


class NameIn(BaseModel):
    name: str


class PinIn(BaseModel):
    pin: str = ""


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


@app.get("/api/state")
def api_state():
    return build_state()


@app.post("/api/join")
def api_join(body: JoinIn):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Add a name first.")
    if len(name) > MAX_NAME:
        raise HTTPException(400, "That name's a bit long - keep it under 40 characters.")
    with connect() as con, con.cursor() as cur:
        drawn, _ = read_state(cur)
        if drawn:
            raise HTTPException(409, "The draw has already happened, so the list is closed.")
        try:
            cur.execute("insert into players (name) values (%s)", (name,))
            con.commit()
        except psycopg.errors.UniqueViolation:
            con.rollback()
            raise HTTPException(409, "Someone's already in with that name.")
    return build_state()


@app.post("/api/leave")
def api_leave(body: NameIn):
    name = (body.name or "").strip()
    with connect() as con, con.cursor() as cur:
        drawn, _ = read_state(cur)
        if drawn:
            raise HTTPException(409, "The draw has already happened.")
        cur.execute("delete from players where lower(name) = lower(%s)", (name,))
        con.commit()
    return build_state()


@app.post("/api/draw")
def api_draw(body: PinIn):
    if body.pin != ORGANISER_PIN:
        raise HTTPException(403, "Wrong organiser PIN.")
    with connect() as con, con.cursor() as cur:
        drawn, _ = read_state(cur)
        if drawn:
            return build_state()  # idempotent
        players = read_players(cur)
        if not players:
            raise HTTPException(400, "No one has joined yet.")
        pool = sorted(TEAMS, key=lambda t: t["rank"])[: len(players)]
        order = players[:]
        random.shuffle(order)
        results = []
        for i, team in enumerate(pool):
            if i < len(order):
                results.append({
                    "player": order[i], "team": team["team"], "group": team["group"],
                    "conf": team["conf"], "rank": team["rank"], "tier": team["tier"],
                    "status": team["status"], "strength": strength(team["rank"]),
                })
        results.sort(key=lambda r: -r["strength"])
        cur.execute("update state set drawn = true, results = %s where id = 1", (Json(results),))
        con.commit()
    return build_state()


@app.post("/api/reset")
def api_reset(body: PinIn):
    if body.pin != ORGANISER_PIN:
        raise HTTPException(403, "Wrong organiser PIN.")
    with connect() as con, con.cursor() as cur:
        cur.execute("delete from players")
        cur.execute("update state set drawn = false, results = '[]'::jsonb where id = 1")
        con.commit()
    return build_state()
