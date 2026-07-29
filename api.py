"""
BookMaker.eu Data API
======================
FastAPI server that serves live, pregame, and futures odds data
scraped from https://lines.bookmaker.eu/en/sports/

Start the server (development):
    uvicorn api:app --reload --port 8055

Start the server (production):
    gunicorn -c gunicorn.conf.py api:app

Endpoints:
    GET /health                         — service health check
    GET /sports                         — list all sports + leagues per data type
    GET /live                           — live in-game odds
    GET /live/{sport}                   — live odds for a specific sport
    GET /pregame                        — upcoming game odds
    GET /pregame/{sport}                — pregame odds for a specific sport
    GET /futures                        — futures / outright winner odds
    GET /futures/{sport}                — futures odds for a specific sport
    GET /history/pregame                — list dates with pregame snapshots
    GET /history/pregame/{date}         — list sports in a pregame snapshot
    GET /history/pregame/{date}/{sport} — historical pregame data
    GET /history/futures                — list dates with futures snapshots
    GET /history/futures/{date}         — historical futures data

Authentication:
    All endpoints (except /health) require header:  X-API-Key: <API_KEY>
    Set the key via environment variable API_KEY (see .env.example).
"""

import os
import json
import time
import threading
from pathlib import Path
from typing import Optional
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Query, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Configuration (from environment variables)
# ---------------------------------------------------------------------------
API_KEY: str = os.environ.get("API_KEY", "")

ALLOWED_ORIGINS: list[str] = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR      = Path(__file__).parent
LIVE_DIR      = BASE_DIR / "live"
PREGAME_DIR   = BASE_DIR / "pregame"
FUTURES_DIR   = BASE_DIR / "futures"
SNAPSHOTS_DIR = BASE_DIR / "snapshots"

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="BookMaker.eu Odds API",
    description="Live, pregame, and futures odds scraped from BookMaker.eu sportsbook.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["X-API-Key", "Content-Type"],
)


# ---------------------------------------------------------------------------
# In-memory cache (TTL = 3 seconds — matches scraper refresh rate)
# ---------------------------------------------------------------------------
_CACHE_TTL = 3.0  # seconds
_cache: dict[str, tuple[float, object]] = {}  # path -> (timestamp, data)
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_dir(base: Path, category_label: str) -> list[dict]:
    """
    Load and merge every {base}/{sport}/{league}.json file.
    Injects a 'sport' field into each record (from the folder name).
    """
    if not base.exists():
        raise HTTPException(
            status_code=503,
            detail=f"{category_label} data directory not found. Run the scraper first.",
        )
    records: list[dict] = []
    for sport_dir in sorted(base.iterdir()):
        if not sport_dir.is_dir():
            continue
        sport = sport_dir.name
        for league_file in sorted(sport_dir.glob("*.json")):
            try:
                data = json.loads(league_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for rec in data:
                        rec.setdefault("sport", sport)
                    records.extend(data)
            except Exception:
                continue
    return records


def _load_live_all() -> list[dict]:
    """Load and merge every live/{sport}/{league}.json file."""
    return _load_dir(LIVE_DIR, "Live")


def _load_pregame_all() -> list[dict]:
    """Load and merge every pregame/{sport}/{league}.json file."""
    return _load_dir(PREGAME_DIR, "Pregame")


def _load_futures_all() -> list[dict]:
    """Load and merge every futures/{sport}/{league}.json file."""
    return _load_dir(FUTURES_DIR, "Futures")


def _sports_index(base: Path) -> dict[str, list[str]]:
    """Return {sport: [league_display_name, ...]} for any category directory."""
    if not base.exists():
        return {}
    index: dict[str, list[str]] = {}
    for sport_dir in sorted(base.iterdir()):
        if not sport_dir.is_dir():
            continue
        leagues = sorted(
            f.stem.replace("_", " ").title()
            for f in sport_dir.glob("*.json")
        )
        if leagues:
            index[sport_dir.name] = leagues
    return index


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _slice_page(items: list, offset: int) -> list:
    """Return items from offset onward (no upper limit)."""
    return items[offset:] if offset else items


# ---------------------------------------------------------------------------
# GET /  (root)
# ---------------------------------------------------------------------------

@app.get("/", summary="API root", tags=["System"])
def root():
    """Welcome page — lists all available endpoints."""
    return {
        "name":        "BookMaker.eu Odds API",
        "version":     "1.0.0",
        "status":      "running",
        "timestamp":   _now_iso(),
        "docs":        "http://localhost:8055/docs",
        "endpoints": {
            "GET /health":                              "Service health check",
            "GET /sports":                              "All sports and leagues index",
            "GET /live":                                "Live in-game odds",
            "GET /live/{sport}":                        "Live odds filtered by sport",
            "GET /pregame":                             "Upcoming pre-game odds",
            "GET /pregame/{sport}":                     "Pre-game odds filtered by sport",
            "GET /futures":                             "Futures / outright winner odds",
            "GET /futures/{sport}":                     "Futures odds filtered by sport",
            "GET /history/pregame":                     "List dates with pregame snapshots",
            "GET /history/pregame/{date}":              "Sports available for a pregame snapshot date",
            "GET /history/pregame/{date}/{sport}":      "Historical pregame data",
            "GET /history/live":                        "List dates with live snapshots",
            "GET /history/live/{date}":                 "Sports available for a live snapshot date",
            "GET /history/live/{date}/{sport}":         "Historical live data",
            "GET /history/futures":                     "List dates with futures snapshots",
            "GET /history/futures/{date}":              "Sports available for a futures snapshot date",
            "GET /history/futures/{date}/{sport}":      "Historical futures data",
        },
        "query_params": {
            "sport":   "Filter by sport  (e.g. soccer, football, baseball)",
            "league":  "Filter by league (e.g. NFL, MLB, Premier League)",
            "market":  "Filter by market (e.g. Moneyline, Spread, Total, Futures)",
            "team":    "Filter by team name (partial match)",
            "offset":  "Pagination offset (default: 0)",
        },
    }


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get("/health", summary="Health check", tags=["System"])
def health_check():
    """Returns 200 if the API is running. No authentication required."""
    return {"status": "ok", "timestamp": _now_iso()}


# ---------------------------------------------------------------------------
# GET /sports
# ---------------------------------------------------------------------------

@app.get("/sports", summary="List all available sports and leagues")
def get_sports():
    """
    Returns a summary of all sports and their leagues available across
    live, pregame, and futures data sources.
    """
    return {
        "fetched_at": _now_iso(),
        "live":    _sports_index(LIVE_DIR),
        "pregame": _sports_index(PREGAME_DIR),
        "futures": _sports_index(FUTURES_DIR),
    }


# ---------------------------------------------------------------------------
# GET /live
# ---------------------------------------------------------------------------

@app.get("/live", summary="Live in-game odds")
def get_live(
    sport: Optional[str] = Query(None, description="Filter by sport (e.g. baseball)"),
    league: Optional[str] = Query(None, description="Filter by league name (e.g. MLB)"),
    market: Optional[str] = Query(None, description="Filter by market type (e.g. Moneyline)"),
    team: Optional[str] = Query(None, description="Filter by team name (partial match)"),
    offset: int = Query(0, ge=0, description="Number of records to skip"),
):
    """
    Returns all current live in-game odds from live/{sport}/{league}.json files.

    **Query parameters (all optional):**
    - `sport` — e.g. `baseball`, `soccer`, `tennis`
    - `league` — e.g. `MLB`, `WNBA`
    - `market` — e.g. `Moneyline`, `Spread`, `Total`
    - `team` — partial team name match
    """
    records = _load_live_all()

    if sport:
        records = [r for r in records if (r.get("sport") or "").lower() == sport.lower()]
    if league:
        records = [r for r in records if league.lower() in (r.get("league") or "").lower()]
    if market:
        records = [r for r in records if (r.get("market") or "").lower() == market.lower()]
    if team:
        records = [
            r for r in records
            if team.lower() in (r.get("home") or "").lower()
            or team.lower() in (r.get("away") or "").lower()
            or team.lower() in (r.get("bet_team") or "").lower()
        ]

    sports_in_result  = sorted({r.get("sport", "unknown") for r in records})
    leagues_in_result = sorted({r.get("league", "Unknown") for r in records})
    total = len(records)
    page  = _slice_page(records, offset)
    return {
        "fetched_at": _now_iso(),
        "total": total,
        "offset": offset,
        "returned": len(page),
        "sports": sports_in_result,
        "leagues": leagues_in_result,
        "data": page,
    }


@app.get("/live/{sport}", summary="Live odds for a specific sport")
def get_live_by_sport(
    sport: str,
    league: Optional[str] = Query(None, description="Filter by league name"),
    market: Optional[str] = Query(None, description="Filter by market type"),
    team: Optional[str] = Query(None, description="Filter by team name (partial match)"),
    offset: int = Query(0, ge=0, description="Number of records to skip"),
):
    return get_live(sport=sport, league=league, market=market, team=team, offset=offset)


# ---------------------------------------------------------------------------
# GET /pregame
# ---------------------------------------------------------------------------

@app.get("/pregame", summary="Upcoming pre-game odds")
def get_pregame(
    sport: Optional[str] = Query(None, description="Filter by sport folder (e.g. soccer, basketball)"),
    league: Optional[str] = Query(None, description="Filter by league name (e.g. EPL, NBA)"),
    market: Optional[str] = Query(None, description="Filter by market type (e.g. Moneyline, Spread)"),
    team: Optional[str] = Query(None, description="Filter by team name (partial match)"),
    offset: int = Query(0, ge=0, description="Number of records to skip"),
):
    """
    Returns upcoming pre-game odds across all leagues.
    Data is refreshed every ~10 seconds by the pregame scraper.

    **Query parameters (all optional):**
    - `sport` — folder name, e.g. `soccer`, `basketball`, `hockey`
    - `league` — e.g. `EPL`, `NBA`, `NHL`
    - `market` — e.g. `Moneyline`, `Spread`, `Total`
    - `team` — partial team name match
    """
    records = _load_pregame_all()

    if sport:
        records = [r for r in records if (r.get("sport") or "").lower() == sport.lower()]
    if league:
        records = [r for r in records if league.lower() in (r.get("league") or "").lower()]
    if market:
        records = [r for r in records if (r.get("market") or "").lower() == market.lower()]
    if team:
        records = [
            r for r in records
            if team.lower() in (r.get("home") or "").lower()
            or team.lower() in (r.get("away") or "").lower()
            or team.lower() in (r.get("bet_team") or "").lower()
        ]

    sports_in_result = sorted({r.get("sport", "unknown") for r in records})
    leagues_in_result = sorted({r.get("league", "Unknown") for r in records})
    total = len(records)
    page = _slice_page(records, offset)
    return {
        "fetched_at": _now_iso(),
        "total": total,
        "offset": offset,
        "returned": len(page),
        "sports": sports_in_result,
        "leagues": leagues_in_result,
        "data": page,
    }


@app.get("/pregame/{sport}", summary="Pre-game odds for a specific sport")
def get_pregame_by_sport(
    sport: str,
    league: Optional[str] = Query(None, description="Filter by league name"),
    market: Optional[str] = Query(None, description="Filter by market type"),
    team: Optional[str] = Query(None, description="Filter by team name (partial match)"),
    offset: int = Query(0, ge=0, description="Number of records to skip"),
):
    return get_pregame(sport=sport, league=league, market=market, team=team, offset=offset)


# ---------------------------------------------------------------------------
# GET /futures
# ---------------------------------------------------------------------------

@app.get("/futures", summary="Futures / outright winner odds")
def get_futures(
    sport: Optional[str] = Query(None, description="Filter by sport folder (e.g. football, baseball)"),
    league: Optional[str] = Query(None, description="Filter by league / competition name (e.g. MLB, NBA)"),
    competition: Optional[str] = Query(None, description="Alias for league (partial match)"),
    market: Optional[str] = Query(None, description="Filter by market name (e.g. Winner)"),
    team: Optional[str] = Query(None, description="Filter by team / participant name (partial match)"),
    participant: Optional[str] = Query(None, description="Alias for team (partial match)"),
    offset: int = Query(0, ge=0, description="Number of records to skip"),
):
    """
    Returns futures / outright odds in the same record format as /pregame.
    Data is refreshed by the futures scraper.

    **Query parameters (all optional):**
    - `sport` — folder name, e.g. `football`, `baseball`
    - `league` / `competition` — e.g. `MLB`, `NBA`
    - `market` — e.g. `Winner`
    - `team` / `participant` — partial team or player name match
    """
    records = _load_futures_all()

    league_filter = league or competition
    team_filter = team or participant

    if sport:
        records = [r for r in records if (r.get("sport") or "").lower() == sport.lower()]
    if league_filter:
        records = [
            r for r in records
            if league_filter.lower() in (r.get("league") or "").lower()
        ]
    if market:
        records = [r for r in records if market.lower() in (r.get("market") or "").lower()]
    if team_filter:
        records = [
            r for r in records
            if team_filter.lower() in (r.get("home") or "").lower()
            or team_filter.lower() in (r.get("away") or "").lower()
            or team_filter.lower() in (r.get("bet_team") or "").lower()
            or team_filter.lower() in (r.get("bet_player") or "").lower()
        ]

    sports_in_result = sorted({r.get("sport", "unknown") for r in records})
    leagues_in_result = sorted({r.get("league", "Unknown") for r in records})
    total = len(records)
    page = _slice_page(records, offset)
    return {
        "fetched_at": _now_iso(),
        "total": total,
        "offset": offset,
        "returned": len(page),
        "sports": sports_in_result,
        "leagues": leagues_in_result,
        "data": page,
    }


@app.get("/futures/{sport}", summary="Futures odds for a specific sport")
def get_futures_by_sport(
    sport: str,
    league: Optional[str] = Query(None, description="Filter by league / competition name"),
    competition: Optional[str] = Query(None, description="Alias for league"),
    market: Optional[str] = Query(None, description="Filter by market name"),
    team: Optional[str] = Query(None, description="Filter by team / participant name"),
    participant: Optional[str] = Query(None, description="Alias for team"),
    offset: int = Query(0, ge=0, description="Number of records to skip"),
):
    return get_futures(
        sport=sport,
        league=league,
        competition=competition,
        market=market,
        team=team,
        participant=participant,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

def _history_dates(category: str) -> list[str]:
    """Return sorted dates (newest first) that have a snapshot for category."""
    if not SNAPSHOTS_DIR.exists():
        return []
    return sorted(
        [
            d.name for d in SNAPSHOTS_DIR.iterdir()
            if d.is_dir() and (d / category).is_dir()
        ],
        reverse=True,
    )


def _load_snapshot_category(date: str, category: str) -> list[dict]:
    """Load and merge all records from snapshots/{date}/{category}/{sport}/*.json."""
    cat_dir = SNAPSHOTS_DIR / date / category
    if not cat_dir.exists():
        raise HTTPException(status_code=404, detail=f"No {category} snapshot for date: {date}")
    return _load_dir(cat_dir, f"Snapshot {date}/{category}")


# ---------------------------------------------------------------------------
# GET /history/pregame
# ---------------------------------------------------------------------------

@app.get("/history/pregame", summary="List dates with pregame snapshots")
def get_pregame_history_dates():
    """Returns all dates (newest first) that have a pregame snapshot."""
    return {"dates": _history_dates("pregame")}


@app.get("/history/pregame/{date}", summary="List pregame sports for a snapshot date")
def get_pregame_history_date(date: str):
    """Returns the sports available in the pregame snapshot for the given date."""
    cat_dir = SNAPSHOTS_DIR / date / "pregame"
    if not cat_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pregame snapshot for date: {date}")
    sports = sorted(d.name for d in cat_dir.iterdir() if d.is_dir())
    return {"date": date, "sports": sports}


@app.get("/history/pregame/{date}/{sport}", summary="Historical pregame data for a date and sport")
def get_pregame_history_sport(
    date: str,
    sport: str,
    league: Optional[str] = Query(None, description="Filter by league name"),
    market: Optional[str] = Query(None, description="Filter by market type"),
    team: Optional[str] = Query(None, description="Filter by team name (partial match)"),
    offset: int = Query(0, ge=0, description="Number of records to skip"),
):
    """Returns all pregame records for a specific sport on a specific date."""
    sport_dir = SNAPSHOTS_DIR / date / "pregame" / sport
    if not sport_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pregame snapshot for {date}/{sport}")

    records: list[dict] = []
    for jf in sorted(sport_dir.glob("*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            if isinstance(data, list):
                records.extend(data)
        except Exception:
            continue

    if league:
        records = [r for r in records if league.lower() in (r.get("league") or "").lower()]
    if market:
        records = [r for r in records if (r.get("market") or "").lower() == market.lower()]
    if team:
        records = [
            r for r in records
            if team.lower() in (r.get("home") or "").lower()
            or team.lower() in (r.get("away") or "").lower()
            or team.lower() in (r.get("bet_team") or "").lower()
        ]

    leagues_in_result = sorted({r.get("league", "Unknown") for r in records})
    total = len(records)
    page  = _slice_page(records, offset)
    return {
        "date": date, "sport": sport,
        "total": total, "offset": offset, "returned": len(page),
        "leagues": leagues_in_result,
        "data": page,
    }


# ---------------------------------------------------------------------------
# GET /history/live
# ---------------------------------------------------------------------------

@app.get("/history/live", summary="List dates with live snapshots")
def get_live_history_dates():
    """Returns all dates (newest first) that have a live snapshot."""
    return {"dates": _history_dates("live")}


@app.get("/history/live/{date}", summary="List live sports for a snapshot date")
def get_live_history_date(date: str):
    """Returns the sports available in the live snapshot for the given date."""
    cat_dir = SNAPSHOTS_DIR / date / "live"
    if not cat_dir.exists():
        raise HTTPException(status_code=404, detail=f"No live snapshot for date: {date}")
    sports = sorted(d.name for d in cat_dir.iterdir() if d.is_dir())
    return {"date": date, "sports": sports}


@app.get("/history/live/{date}/{sport}", summary="Historical live data for a date and sport")
def get_live_history_sport(
    date: str,
    sport: str,
    league: Optional[str] = Query(None, description="Filter by league name"),
    market: Optional[str] = Query(None, description="Filter by market type"),
    team: Optional[str] = Query(None, description="Filter by team name (partial match)"),
    offset: int = Query(0, ge=0, description="Number of records to skip"),
):
    """Returns all live records for a specific sport on a specific date."""
    sport_dir = SNAPSHOTS_DIR / date / "live" / sport
    if not sport_dir.exists():
        raise HTTPException(status_code=404, detail=f"No live snapshot for {date}/{sport}")

    records: list[dict] = []
    for jf in sorted(sport_dir.glob("*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            if isinstance(data, list):
                records.extend(data)
        except Exception:
            continue

    if league:
        records = [r for r in records if league.lower() in (r.get("league") or "").lower()]
    if market:
        records = [r for r in records if (r.get("market") or "").lower() == market.lower()]
    if team:
        records = [
            r for r in records
            if team.lower() in (r.get("home") or "").lower()
            or team.lower() in (r.get("away") or "").lower()
            or team.lower() in (r.get("bet_team") or "").lower()
        ]

    total = len(records)
    page  = _slice_page(records, offset)
    return {
        "date": date, "sport": sport,
        "total": total, "offset": offset, "returned": len(page),
        "data": page,
    }


# ---------------------------------------------------------------------------
# GET /history/futures
# ---------------------------------------------------------------------------

@app.get("/history/futures", summary="List dates with futures snapshots")
def get_futures_history_dates():
    """Returns all dates (newest first) that have a futures snapshot."""
    return {"dates": _history_dates("futures")}


@app.get("/history/futures/{date}", summary="List futures sports for a snapshot date")
def get_futures_history_date(date: str):
    """Returns the sports available in the futures snapshot for the given date."""
    cat_dir = SNAPSHOTS_DIR / date / "futures"
    if not cat_dir.exists():
        raise HTTPException(status_code=404, detail=f"No futures snapshot for date: {date}")
    sports = sorted(d.name for d in cat_dir.iterdir() if d.is_dir())
    return {"date": date, "sports": sports}


@app.get("/history/futures/{date}/{sport}", summary="Historical futures data for a date and sport")
def get_futures_history_sport(
    date: str,
    sport: str,
    league: Optional[str] = Query(None, description="Filter by league name"),
    market: Optional[str] = Query(None, description="Filter by market type"),
    team: Optional[str] = Query(None, description="Filter by team name (partial match)"),
    offset: int = Query(0, ge=0, description="Number of records to skip"),
):
    """Returns all futures records for a specific sport on a specific date."""
    sport_dir = SNAPSHOTS_DIR / date / "futures" / sport
    if not sport_dir.exists():
        raise HTTPException(status_code=404, detail=f"No futures snapshot for {date}/{sport}")

    records: list[dict] = []
    for jf in sorted(sport_dir.glob("*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            if isinstance(data, list):
                records.extend(data)
        except Exception:
            continue

    if league:
        records = [r for r in records if league.lower() in (r.get("league") or "").lower()]
    if market:
        records = [r for r in records if market.lower() in (r.get("market") or "").lower()]
    if team:
        records = [
            r for r in records
            if team.lower() in (r.get("home") or "").lower()
            or team.lower() in (r.get("away") or "").lower()
            or team.lower() in (r.get("bet_team") or "").lower()
            or team.lower() in (r.get("bet_player") or "").lower()
        ]

    total = len(records)
    page  = _slice_page(records, offset)
    return {
        "date": date, "sport": sport,
        "total": total, "offset": offset, "returned": len(page),
        "data": page,
    }


# ---------------------------------------------------------------------------
# Run directly:  python api.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8055, reload=False)
