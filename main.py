"""
World Cup 2026 sweepstake - standalone mini-app (Supabase + live results).

Friends open the page and add their own names. The organiser (with a PIN) runs
the draw: the strongest N teams are handed out one each (N = number of players),
the rest sit it out. Once the tournament starts, the page shows who's still in,
who's out, and a leaderboard by how far each team has gone.

State is stored in Supabase (Postgres). Live scores come from API-Football and
are cached server-side so we stay inside the free 100-requests-a-day limit.

Environment variables (set these where you deploy):
  DATABASE_URL         Supabase connection string (Session pooler, URI form)
  FOOTBALL_DATA_TOKEN  your football-data.org token (no token = no live scores)
  ORGANISER_PIN        the PIN that unlocks Draw/Reset (default: 1966)
"""
import hashlib
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
FD_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "")
ORGANISER_PIN = os.environ.get("ORGANISER_PIN", "1966")
MAX_NAME = 40

API_BASE = "https://api.football-data.org/v4"
COMPETITION = "WC"     # FIFA World Cup
LIVE_TTL = 900         # seconds between live refreshes (free tier is 10/min - we use ~1 per 15 min)

# Mini-league scoring (applied per match, to each player's team)
PTS_GOAL = 1
PTS_CONCEDE = -1
PTS_CLEAN_SHEET = 1
PTS_WIN = 3
PTS_DRAW = 1
KO_MULTIPLIER = 2      # points double from the Round of 32 onwards

app = FastAPI(title="World Cup 2026 Sweepstake")


def strength(rank: int) -> int:
    return 49 - rank


# ----------------------------------------------------------------------------
# Name matching: football-data.org names -> our team names
# ----------------------------------------------------------------------------
def normalise(name: str) -> str:
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(c for c in n if not unicodedata.combining(c)).lower().strip()
    n = re.sub(r"[^a-z0-9 ]", "", n)
    return re.sub(r"\s+", " ", n)


MY_NORM = {normalise(t["team"]): t["team"] for t in TEAMS}

# football-data.org / common spellings -> our normalised name.
# Kept deliberately broad; harmless extra entries, and easy to extend if a
# team shows up unmatched after go-live.
ALIASES = {
    "united states": "usa",
    "united states of america": "usa",
    "korea republic": "south korea",
    "republic of korea": "south korea",
    "ir iran": "iran",
    "iran islamic republic": "iran",
    "turkey": "turkiye",
    "czech republic": "czechia",
    "cote divoire": "ivory coast",
    "ivory coast": "ivory coast",
    "cabo verde": "cape verde",
    "cape verde islands": "cape verde",
    "congo dr": "dr congo",
    "dr congo": "dr congo",
    "democratic republic of the congo": "dr congo",
    "democratic republic of congo": "dr congo",
    "bosnia herzegovina": "bosnia and herzegovina",
    "bosniaherzegovina": "bosnia and herzegovina",
    "curacao": "curacao",
}


def display_for(api_name: str):
    n = normalise(api_name)
    n = ALIASES.get(n, n)
    return MY_NORM.get(n)


STAGE_LABEL = {1: "Group stage", 2: "Round of 32", 3: "Round of 16",
               4: "Quarter-finals", 5: "Semi-finals", 6: "Final", 7: "Champion"}
FINISHED = {"FINISHED", "AWARDED"}
UPCOMING = {"SCHEDULED", "TIMED", "IN_PLAY", "PAUSED"}


def stage_rank(stage: str) -> int:
    s = (stage or "").upper()
    if "GROUP" in s:
        return 1
    if "LAST_32" in s or "ROUND_OF_32" in s:
        return 2
    if "LAST_16" in s or "ROUND_OF_16" in s:
        return 3
    if "QUARTER" in s:
        return 4
    if "SEMI" in s:
        return 5
    if "THIRD" in s or "3RD" in s:
        return 5          # third-place match = reached the semis
    if "FINAL" in s:
        return 6
    return 1


def compute_live(matches):
    """Pure function: football-data.org matches -> per-team state. Returns
    (teams_data, started). teams_data maps our team name -> dict with
    live ('in'/'out'/'champion'), stage_rank, stage label, last result,
    league points, and next fixture."""
    data = {}
    group_unfinished = False
    any_knockout = False
    started = False

    for m in matches:
        rank = stage_rank(m.get("stage", ""))
        if rank >= 2:
            any_knockout = True
        status = m.get("status", "")
        finished = status in FINISHED
        if finished:
            started = True
        date = m.get("utcDate", "")
        home = m.get("homeTeam") or {}
        away = m.get("awayTeam") or {}
        score = m.get("score") or {}
        ft = score.get("fullTime") or {}
        winner = score.get("winner")          # HOME_TEAM / AWAY_TEAM / DRAW / None
        hg, ag = ft.get("home"), ft.get("away")

        sides = [
            (home, away, hg, ag, "HOME_TEAM"),
            (away, home, ag, hg, "AWAY_TEAM"),
        ]
        for me, opp, gf, ga, side in sides:
            name = display_for(me.get("name", ""))
            if not name:
                continue
            info = data.setdefault(name, {
                "stage_rank": 1, "eliminated": False, "champion": False,
                "last": None, "last_date": "", "points": 0,
                "next": None, "next_date": "",
            })
            if rank > info["stage_rank"]:
                info["stage_rank"] = rank

            if finished and gf is not None and ga is not None:
                if winner == side:
                    result = "win"
                elif winner == "DRAW":
                    result = "draw"
                elif winner in ("HOME_TEAM", "AWAY_TEAM"):
                    result = "loss"
                else:                          # no winner field - fall back to goals
                    result = "win" if gf > ga else ("draw" if gf == ga else "loss")

                pts = gf * PTS_GOAL + ga * PTS_CONCEDE
                if ga == 0:
                    pts += PTS_CLEAN_SHEET
                if result == "win":
                    pts += PTS_WIN
                elif result == "draw":
                    pts += PTS_DRAW
                if rank >= 2:                  # knockout multiplier
                    pts *= KO_MULTIPLIER
                info["points"] += pts

                opp_name = display_for(opp.get("name", "")) or opp.get("name", "?")
                verb = {"win": "beat", "draw": "drew with", "loss": "lost to"}[result]
                if date >= info["last_date"]:
                    info["last_date"] = date
                    info["last"] = f"{verb} {opp_name} {gf}-{ga}"

                if rank >= 2 and result == "loss":
                    info["eliminated"] = True
                if rank == 6 and result == "win":
                    info["champion"] = True
            else:
                if rank == 1 and status in UPCOMING:
                    group_unfinished = True
                if status in UPCOMING:
                    opp_name = display_for(opp.get("name", "")) or opp.get("name", "?")
                    if not info["next_date"] or date < info["next_date"]:
                        info["next_date"] = date
                        info["next"] = {"opp": opp_name, "utc": date,
                                        "live": status in ("IN_PLAY", "PAUSED")}

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
            "points": info["points"],
            "next": None if status in ("out", "champion") else info["next"],
        }
    return out, started


# ----------------------------------------------------------------------------
# Live results cache
# ----------------------------------------------------------------------------
_live = {"at": 0.0, "data": {}, "started": False, "error": None, "fetched": False, "matches": []}


def get_live():
    if not FD_TOKEN:
        return {"data": {}, "started": False, "updated": None, "error": "no_key"}
    now = time.time()
    if _live["fetched"] and now - _live["at"] < LIVE_TTL:
        return {"data": _live["data"], "started": _live["started"],
                "updated": _live["at"], "error": _live["error"]}
    try:
        r = httpx.get(f"{API_BASE}/competitions/{COMPETITION}/matches",
                      headers={"X-Auth-Token": FD_TOKEN}, timeout=15)
        if r.status_code != 200:
            _live["error"] = "api_error"
            _live["at"] = now            # back off; don't hammer the API on errors
        else:
            matches = (r.json() or {}).get("matches", [])
            data, started = compute_live(matches)
            _live.update({"data": data, "started": started, "error": None,
                          "at": now, "fetched": True, "matches": matches})
    except Exception:
        _live["error"] = "unreachable"
        _live["at"] = now
    return {"data": _live["data"], "started": _live["started"],
            "updated": _live["at"] if _live["fetched"] else None, "error": _live["error"]}


# ----------------------------------------------------------------------------
# Per-team detail (for the team page)
# ----------------------------------------------------------------------------
def team_detail(team_name, matches):
    """Build one team's record, group position, recent form and full match list
    from the football-data.org matches we already cache. Pure function."""
    my = team_name
    tinfo = next((t for t in TEAMS if t["team"] == my), None)
    group_letter = tinfo["group"] if tinfo else None

    crest = ""
    played = won = drawn = lost = gf = ga = 0
    form = []
    mlist = []

    rows = []
    for m in matches:
        home = m.get("homeTeam") or {}
        away = m.get("awayTeam") or {}
        if display_for(home.get("name", "")) == my:
            rows.append((m, home, away, "HOME_TEAM"))
        elif display_for(away.get("name", "")) == my:
            rows.append((m, away, home, "AWAY_TEAM"))
    rows.sort(key=lambda r: r[0].get("utcDate", ""))

    for m, me_side, opp_side, my_token in rows:
        if not crest:
            crest = me_side.get("crest", "") or ""
        score = m.get("score") or {}
        ft = score.get("fullTime") or {}
        winner = score.get("winner")
        if my_token == "HOME_TEAM":
            mg, og = ft.get("home"), ft.get("away")
        else:
            mg, og = ft.get("away"), ft.get("home")
        status = m.get("status", "")
        rank = stage_rank(m.get("stage", ""))
        finished = status in FINISHED and mg is not None and og is not None
        result = None
        if finished:
            if winner == my_token:
                result = "W"
            elif winner == "DRAW":
                result = "D"
            elif winner in ("HOME_TEAM", "AWAY_TEAM"):
                result = "L"
            else:
                result = "W" if mg > og else ("D" if mg == og else "L")
            played += 1
            gf += mg
            ga += og
            won += result == "W"
            drawn += result == "D"
            lost += result == "L"
            form.append(result)
        mlist.append({
            "stage": STAGE_LABEL[min(rank, 7)],
            "opp": display_for(opp_side.get("name", "")) or opp_side.get("name", "?"),
            "oppCrest": opp_side.get("crest", "") or "",
            "utc": m.get("utcDate", ""), "status": status,
            "gf": mg, "ga": og, "result": result, "finished": finished,
        })

    # Crest lookup for every team that has appeared in the feed.
    crest_by = {}
    for m in matches:
        for side in ("homeTeam", "awayTeam"):
            s = m.get(side) or {}
            nm = display_for(s.get("name", ""))
            if nm and s.get("crest") and nm not in crest_by:
                crest_by[nm] = s["crest"]
    if not crest:
        crest = crest_by.get(my, "")

    # Group table: built from this group's finished matches. All teams in the group
    # are shown (including any yet to play). Powers the position line and mini-table.
    group_pos = group_size = None
    group_table = []
    if group_letter:
        gkey = "GROUP_" + group_letter
        tbl = {}

        def blank(nm):
            return {"team": nm, "p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0}

        for m in matches:
            if (m.get("group") or "") != gkey or m.get("status") not in FINISHED:
                continue
            ft = (m.get("score") or {}).get("fullTime") or {}
            h, a = ft.get("home"), ft.get("away")
            if h is None or a is None:
                continue
            hn = display_for((m.get("homeTeam") or {}).get("name", "")) or "?"
            an = display_for((m.get("awayTeam") or {}).get("name", "")) or "?"
            for nm, gfor, gag in ((hn, h, a), (an, a, h)):
                r = tbl.setdefault(nm, blank(nm))
                r["p"] += 1
                r["gf"] += gfor
                r["ga"] += gag
                if gfor > gag:
                    r["w"] += 1
                    r["pts"] += 3
                elif gfor == gag:
                    r["d"] += 1
                    r["pts"] += 1
                else:
                    r["l"] += 1
        for t in TEAMS:                          # include teams yet to kick off
            if t["group"] == group_letter and t["team"] not in tbl:
                tbl[t["team"]] = blank(t["team"])
        group_size = len([t for t in TEAMS if t["group"] == group_letter]) or len(tbl) or None
        ranked = sorted(tbl.values(), key=lambda r: (r["pts"], r["gf"] - r["ga"], r["gf"]), reverse=True)
        for i, r in enumerate(ranked):
            r["gd"] = r["gf"] - r["ga"]
            r["pos"] = i + 1
            r["crest"] = crest_by.get(r["team"], "")
        group_table = ranked
        group_pos = next((r["pos"] for r in ranked if r["team"] == my), None)

    return {
        "team": my, "crest": crest, "group": group_letter,
        "record": {"played": played, "won": won, "drawn": drawn, "lost": lost,
                   "gf": gf, "ga": ga, "gd": gf - ga},
        "groupPos": group_pos, "groupSize": group_size, "groupTable": group_table,
        "form": form[-5:], "matches": mlist,
    }


# ----------------------------------------------------------------------------
# Banter feed (personalised, result-driven)
# ----------------------------------------------------------------------------
# Keyed by lowercased player name. Lines are picked by a stable hash of the
# player + match date, so a given result always reads the same (no flip-flop on
# poll) but different results / days vary. Pure in-jokes from the group - keep.
PERSONAS = {
    "arran": {
        "win":  ["Lifelong fan since roughly kickoff.",
                 "The bandwagon's got a new favourite and Arran's driving it."],
        "loss": ["He's already scouting whoever's top to support next.",
                 "Allegiance withdrawn, effective immediately."],
        "draw": ["Arran's keeping his options open, as ever."],
    },
    "pally": {
        "win":  ["Years of Arsenal pain and now he won't shut up.",
                 "He's somehow crediting Arteta for this."],
        "loss": ["Back to familiar Arsenal territory - hope, then heartbreak.",
                 "An Arsenal fan handling a loss: well rehearsed."],
        "draw": ["Very Arsenal - so nearly, but not quite."],
    },
    "amo": {
        "win":  ["First bit of joy since United were last good.",
                 "Amo almost smiled. Almost."],
        "loss": ["Amo's not even flinching - United desensitised him years ago.",
                 "He's seen worse every weekend, frankly."],
        "draw": ["Amo shrugs. He's well used to mid."],
    },
    "vimz": {
        "win":  ["That's another pint sorted.",
                 "Celebrating the only way he knows - a cold one."],
        "loss": ["He'll drown it in a pint. Villa trained him for this.",
                 "Another beer, another sorrow drowned."],
        "draw": ["A draw's worth a pint too, in fairness."],
    },
    "pete": {
        "win":  ["Peetu celebrates with a lamb feast and zero showers.",
                 "Peetu's buzzing - still not showering though."],
        "loss": ["Peetu consoles himself with more lamb. Shower remains off the table.",
                 "Gutted, but there's always lamb."],
        "draw": ["Peetu shrugs and reaches for the lamb."],
    },
    "sana d": {
        "win":  ["The undercover fed's cover holds another day.",
                 "Nothing to see here - the fed got the job done."],
        "loss": ["Even the fed couldn't pull strings for this one.",
                 "Internal investigation pending."],
        "draw": ["The fed neither confirms nor denies that result."],
    },
    "munny": {
        "win":  ["A fruity little win for Munny.",
                 "Munny's celebrating. Fruitily."],
        "loss": ["Tough one - fruity scenes turned sour.",
                 "Munny's gutted. We'll leave him to it."],
        "draw": ["A fruity stalemate for Munny."],
    },
    "kyle": {
        "win":  ["Another 'cultural trip' to Amsterdam incoming.",
                 "Kyle's buzzing - and you know exactly why."],
        "loss": ["Kyle's down, but Amsterdam always cheers him up.",
                 "He'll cope. He has his ways."],
        "draw": ["Kyle's very relaxed about it. Very."],
    },
    "bhav": {
        "win":  ["A United fan remembering what winning feels like.",
                 "Bhav's enjoying this rare sensation - a win."],
        "loss": ["Bhav takes it with the calm of a seasoned United sufferer.",
                 "Just another weekend for a United fan."],
        "draw": ["Bhav's seen enough draws to not care."],
    },
    "gurpreet saini": {
        "win":  ["Wala wala! Gurpreet's rolling a baseball bat to celebrate.",
                 "Sparking up a celebratory baseball bat. Wala wala."],
        "loss": ["Gurpreet's lighting a baseball bat to cope. Wala wala.",
                 "Wala wala - rolling his sorrows away."],
        "draw": ["Gurpreet shrugs, rolls another. Wala wala."],
    },
    "manni": {
        "win":  ["Fruity celebrations all round for Manni.",
                 "Manni and Munny, two fruity peas in a pod, both buzzing."],
        "loss": ["Manni and Munny commiserating together, fruitily.",
                 "A fruity disappointment for Manni."],
        "draw": ["Manni's fine with a draw - fruity equilibrium."],
    },
}


def _pick(pool, seed):
    if not pool:
        return ""
    h = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16)
    return pool[h % len(pool)]


def banter_line(player, team, opp, gf, ga, result, mdate):
    gf = gf or 0
    ga = ga or 0
    margin = abs(gf - ga)
    if result == "W":
        verb = "thrashed" if margin >= 3 else ("beat" if margin >= 2 else "edged")
        pol = "win"
    elif result == "L":
        verb = "got hammered by" if margin >= 3 else ("lost to" if margin >= 2 else "lost narrowly to")
        pol = "loss"
    else:
        verb = "drew with"
        pol = "draw"
    fact = f"{player}'s {team} {verb} {opp} {gf}\u2013{ga}."
    flav = _pick(PERSONAS.get(player.strip().lower(), {}).get(pol, []), player.lower() + "|" + mdate)
    return (fact + " " + flav).strip()


def build_feed(rows, matches):
    """One banter line per player, for their most recent finished match, newest first."""
    owner = {r["team"]: r["player"] for r in rows if r.get("player")}
    out = []
    for team, player in owner.items():
        best = None
        for m in matches:
            if m.get("status") not in FINISHED:
                continue
            hn = display_for((m.get("homeTeam") or {}).get("name", ""))
            an = display_for((m.get("awayTeam") or {}).get("name", ""))
            if team not in (hn, an):
                continue
            ft = (m.get("score") or {}).get("fullTime") or {}
            h, a = ft.get("home"), ft.get("away")
            if h is None or a is None:
                continue
            mdate = m.get("utcDate", "")
            if best and mdate <= best["mdate"]:
                continue
            if hn == team:
                gf, ga, opp = h, a, an
            else:
                gf, ga, opp = a, h, hn
            res = "W" if gf > ga else ("D" if gf == ga else "L")
            best = {"mdate": mdate, "gf": gf, "ga": ga, "opp": opp, "res": res}
        if best:
            out.append((best["mdate"], {
                "text": banter_line(player, team, best["opp"], best["gf"], best["ga"], best["res"], best["mdate"]),
                "tone": {"W": "good", "L": "bad", "D": "meh"}[best["res"]],
            }))
    out.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in out[:8]]


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
        cur.execute("alter table state add column if not exists meta jsonb not null default '{}'::jsonb")
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


def read_meta(cur):
    cur.execute("select meta from state where id = 1")
    row = cur.fetchone()
    return (row[0] or {}) if row else {}


def write_meta(meta):
    with connect() as con, con.cursor() as cur:
        cur.execute("update state set meta = %s where id = 1", (Json(meta),))
        con.commit()


def build_state():
    with connect() as con, con.cursor() as cur:
        players = read_players(cur)
        drawn, results = read_state(cur)
        meta = read_meta(cur)

    live = get_live()
    started = live["started"]
    ld = live["data"]
    for row in results:
        info = ld.get(row["team"])
        if info:
            row["live"] = info["live"]
            row["stage"] = info["stage"]
            row["last"] = info["last"]
            row["points"] = info["points"]
            row["next"] = info["next"]
        else:
            row["points"] = 0
            row["next"] = None

    if results and started and ld:
        def key(r):
            info = ld.get(r["team"], {})
            return (r.get("points", 0), info.get("stage_rank", 1), r["strength"])
        results = sorted(results, key=key, reverse=True)
    else:
        results = sorted(results, key=lambda r: -r["strength"])

    # Daily position movement (the arrows). Baseline = positions at the start of
    # today (UTC). It's set on the first state-build of each new day and held until
    # the next, so move = how far a player has climbed or slid since this morning.
    if results and started:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        cur_pos = {r["player"]: i + 1 for i, r in enumerate(results)}
        if meta.get("day") != today:
            meta = {"day": today, "ranks": cur_pos}
            try:
                write_meta(meta)
            except Exception:
                pass
        baseline = meta.get("ranks", {})
        for i, r in enumerate(results):
            b = baseline.get(r["player"])
            r["move"] = (b - (i + 1)) if isinstance(b, int) else 0
    else:
        for r in results:
            r["move"] = 0

    return {
        "players": players, "count": len(players), "drawn": drawn,
        "results": results, "started": started,
        "liveUpdated": live["updated"], "liveError": live["error"],
        "totalTeams": len(TEAMS),
        "feed": build_feed(results, _live.get("matches", [])) if started else [],
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


@app.get("/api/team/{name}")
def api_team(name: str):
    get_live()                       # warm the match cache
    detail = team_detail(name, _live.get("matches", []))
    detail["started"] = _live.get("started", False)
    detail["liveError"] = _live.get("error")
    return detail


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
