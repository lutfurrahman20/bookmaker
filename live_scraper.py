#!/usr/bin/env python3
"""
BookMaker.eu LIVE games scraper
Scrapes games that are currently in progress (start_date < now UTC)
from https://lines.bookmaker.eu/en/sports/

Usage:
    python live_scraper.py                 # print JSON to stdout
    python live_scraper.py -o live.json    # save to file
    python live_scraper.py --delay 0.5     # faster requests
    python live_scraper.py --indent 0      # compact JSON
    python live_scraper.py --list-urls     # preview URLs
"""

import argparse
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

BASE_URL  = "https://lines.bookmaker.eu"
BOOK_NAME = "BookMaker"
PT_TZ     = ZoneInfo("America/Los_Angeles")
UTC_TZ    = ZoneInfo("UTC")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://lines.bookmaker.eu/",
}

# Keywords that mark a futures/props URL — these are skipped entirely
_FUTURES_KEYWORDS = {
    "futures", "awards", "make-the-playoffs", "regular-season-wins",
    "regular-season-pts", "season-wins", "season-projections",
    "stage-of-elimination", "passing-stats", "rushing-stats",
    "defensive-stats", "receiving-stats", "rbi-stats", "home-runs-stats",
    "strikeouts-stats", "stat-leaders", "grand-salami", "winner",
    "golden-ball", "elections", "debate-props", "wsl-futures",
    "f1-futures", "f1-championship", "f1-constructors",
    "nascar-cup-series", "usac-racing", "high-limit-racing",
    "emmy-awards", "entertainment-props",
}

# ─── Team abbreviation map ────────────────────────────────────────────────────

_ABBREV: Dict[str, str] = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL",
    "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN",
    "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND",
    "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE",
    "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI",
    "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL", "Detroit Tigers": "DET",
    "Houston Astros": "HOU", "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Athletics": "OAK",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD", "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB", "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR", "Washington Nationals": "WAS",
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS",
    "Brooklyn Nets": "BKN", "Charlotte Hornets": "CHA",
    "Cleveland Cavaliers": "CLE", "Dallas Mavericks": "DAL",
    "Denver Nuggets": "DEN", "Golden State Warriors": "GS",
    "Indiana Pacers": "IND", "Los Angeles Clippers": "LAC",
    "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA", "Milwaukee Bucks": "MIL",
    "New Orleans Pelicans": "NO", "New York Knicks": "NYK",
    "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC",
    "San Antonio Spurs": "SA", "Toronto Raptors": "TOR",
    "Utah Jazz": "UTA", "Washington Wizards": "WAS",
    "Anaheim Ducks": "ANA", "Boston Bruins": "BOS",
    "Buffalo Sabres": "BUF", "Calgary Flames": "CGY",
    "Carolina Hurricanes": "CAR", "Chicago Blackhawks": "CHI",
    "Colorado Avalanche": "COL", "Columbus Blue Jackets": "CBJ",
    "Dallas Stars": "DAL", "Detroit Red Wings": "DET",
    "Edmonton Oilers": "EDM", "Florida Panthers": "FLA",
    "Los Angeles Kings": "LA", "Minnesota Wild": "MIN",
    "Montreal Canadiens": "MTL", "Nashville Predators": "NSH",
    "New Jersey Devils": "NJ", "New York Islanders": "NYI",
    "New York Rangers": "NYR", "Ottawa Senators": "OTT",
    "Philadelphia Flyers": "PHI", "Pittsburgh Penguins": "PIT",
    "San Jose Sharks": "SJ", "Seattle Kraken": "SEA",
    "St. Louis Blues": "STL", "Tampa Bay Lightning": "TB",
    "Toronto Maple Leafs": "TOR", "Vancouver Canucks": "VAN",
    "Vegas Golden Knights": "VGK", "Washington Capitals": "WAS",
    "Winnipeg Jets": "WPG",
}

_STOP = {"de","del","la","le","los","las","the","of","fc","ac","sc","ss","as","us","afc","bk"}


def _abbrev(name: str) -> str:
    if name in _ABBREV:
        return _ABBREV[name]
    words = re.sub(r"['\-\.]", " ", name).split()
    sig = [w for w in words if w.lower() not in _STOP and w]
    if not sig:
        sig = words
    if len(sig) == 1:
        return sig[0][:4].upper()
    if len(sig) == 2:
        a, b = sig
        return (a[:3] if len(a) >= 3 else a + b[:max(1, 4 - len(a))]).upper()
    return "".join(w[0] for w in sig[:4]).upper()


# ─── Value parsers ────────────────────────────────────────────────────────────

_FRAC = {"½": ".5", "¼": ".25", "¾": ".75", "\u00bd": ".5"}


def _norm(raw: str) -> str:
    for k, v in _FRAC.items():
        raw = raw.replace(k, v)
    return raw.replace("\\", "").strip()


def _parse_odds(raw: str) -> Optional[int]:
    raw = _norm(raw)
    if not raw or raw in ("-", "—", "Ended"):
        return None
    try:
        v = int(float(raw.replace("+", "")))
        return None if v == -99999 else v
    except ValueError:
        return None


def _parse_line(raw: str) -> Optional[float]:
    raw = _norm(raw)
    if not raw or raw in ("-", "—", "Ended", "-99999"):
        return None
    try:
        return float(raw.replace("+", ""))
    except ValueError:
        return None


# ─── Date / time parser ───────────────────────────────────────────────────────

_TITLE_RE = re.compile(
    r"START\s+(\d{1,2})/(\d{2})\s+(\d{1,2}:\d{2}\s*(?:am|pm))\s*PT",
    re.IGNORECASE,
)


def _parse_start_date(title: str) -> Optional[str]:
    m = _TITLE_RE.search(title)
    if not m:
        return None
    month, day, tpart = int(m.group(1)), int(m.group(2)), m.group(3).strip()
    now = datetime.now(tz=PT_TZ)
    year = now.year
    fmt = "%Y-%m-%d %I:%M%p"
    try:
        dt = datetime.strptime(f"{year}-{month:02d}-{day:02d} {tpart}", fmt).replace(tzinfo=PT_TZ)
    except ValueError:
        return None
    if (dt - now).total_seconds() < -86400:
        year += 1
        try:
            dt = datetime.strptime(f"{year}-{month:02d}-{day:02d} {tpart}", fmt).replace(tzinfo=PT_TZ)
        except ValueError:
            return None
    return dt.astimezone(UTC_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ─── URL discovery (game_line pages only) ────────────────────────────────────

def _discover_game_urls(session: requests.Session) -> List[Tuple[str, str]]:
    """Return (url, label) for all game-line pages in the navigation."""
    try:
        resp = session.get(f"{BASE_URL}/en/sports/", headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Could not load navigation: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if href.startswith("http"):
            if not href.startswith(BASE_URL):
                continue
            path = href[len(BASE_URL):]
        else:
            path = href

        if not path.startswith("/en/sports/"):
            continue
        if "/events/" in path:
            continue
        path_norm = path if path.endswith("/") else path + "/"
        parts = path_norm.strip("/").split("/")
        if len(parts) <= 3:          # skip bare /en/sports/{sport}/ pages
            continue
        if path in seen:
            continue
        slug = path.rstrip("/").split("/")[-1]
        if any(kw in slug for kw in _FUTURES_KEYWORDS):
            continue                 # skip futures pages
        seen.add(path)
        results.append((f"{BASE_URL}{path}", a.get_text(strip=True)))

    return results


# ─── HTML helpers ─────────────────────────────────────────────────────────────

def _tag_text(tag: Optional[Tag]) -> str:
    return tag.get_text(strip=True) if tag else ""


def _league_from_subtitle(th: Tag) -> Tuple[str, Optional[str]]:
    parts: List[str] = []
    buf: List[str] = []
    for node in th.children:
        if isinstance(node, NavigableString):
            t = str(node).strip()
            if t:
                buf.append(t)
        elif isinstance(node, Tag):
            if node.name == "br":
                s = " ".join(buf)
                if s:
                    parts.append(s)
                buf = []
            else:
                buf.append(node.get_text(strip=True))
    s = " ".join(buf)
    if s:
        parts.append(s)
    league = parts[0] if parts else th.get_text(strip=True)
    venue  = parts[1] if len(parts) > 1 else None
    return league, venue


# ─── Game-line table parser ───────────────────────────────────────────────────

def _parse_game_table(soup: BeautifulSoup, url: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    table = soup.find("table")
    if not table:
        return records

    game_league: Dict[int, str] = {}
    game_tourn:  Dict[int, Optional[str]] = {}
    cur_league = url.rstrip("/").split("/")[-1].replace("-", " ").upper()
    cur_tourn: Optional[str] = None

    for row in table.find_all("tr"):
        sub = row.find("th", class_="oddsSubTitle")
        if sub:
            cur_league, cur_tourn = _league_from_subtitle(sub)
            continue
        ttd = row.find("td", id=re.compile(r"^Game\d+_Time$"))
        if ttd:
            gm = re.match(r"Game(\d+)_Time", ttd["id"])
            if gm:
                n = int(gm.group(1))
                game_league[n] = cur_league
                game_tourn[n]  = cur_tourn

    for ttd in table.find_all("td", id=re.compile(r"^Game\d+_Time$")):
        gm = re.match(r"Game(\d+)_Time", ttd["id"])
        if not gm:
            continue
        n    = gm.group(1)
        nint = int(n)

        league     = game_league.get(nint, cur_league)
        tournament = game_tourn.get(nint)
        start_date = _parse_start_date(ttd.get("title", ""))

        def td(pfx: str) -> Optional[Tag]:
            return table.find("td", id=f"{pfx}{n}")  # type: ignore[return-value]

        away = _tag_text(td("vN_"))
        home = _tag_text(td("hN_"))
        if not away or not home:
            continue

        away_short = _abbrev(away)
        home_short = _abbrev(home)
        away_ml    = _parse_odds(_tag_text(td("vM_")))
        home_ml    = _parse_odds(_tag_text(td("hM_")))
        away_sp    = _parse_line(_tag_text(td("vS_")))
        home_sp    = _parse_line(_tag_text(td("hS_")))
        total      = _parse_line(_tag_text(td("vT_")))
        draw_td    = td("dN_")
        draw_ml    = _parse_odds(_tag_text(td("dM_"))) if draw_td else None

        base: Dict[str, Any] = {
            "limit": None, "tournament": tournament, "betlink": None,
            "sgp": None, "league": league, "bet_occurence": None,
            "start_date": start_date, "home_short": home_short,
            "internal_betlink": None, "away_brief": away_short,
            "bet_player": None, "home": home, "home_brief": home_short,
            "is_main": True, "away": away, "away_short": away_short,
            "book": BOOK_NAME,
        }

        if away_ml is not None:
            records.append({**base, "am_odds": away_ml, "bet_team": away,   "market": "Moneyline", "line": None})
        if home_ml is not None:
            records.append({**base, "am_odds": home_ml, "bet_team": home,   "market": "Moneyline", "line": None})
        if draw_ml is not None:
            records.append({**base, "am_odds": draw_ml, "bet_team": "Draw", "market": "Moneyline", "line": None})
        if away_sp is not None:
            records.append({**base, "am_odds": -110, "bet_team": away, "market": "Spread", "line": away_sp})
        if home_sp is not None:
            records.append({**base, "am_odds": -110, "bet_team": home, "market": "Spread", "line": home_sp})
        if total is not None:
            records.append({**base, "am_odds": -110, "bet_team": "Over",  "market": "Total", "line": total})
            records.append({**base, "am_odds": -110, "bet_team": "Under", "market": "Total", "line": total})

    return records


# ─── Network ──────────────────────────────────────────────────────────────────

def _fetch(url: str, session: requests.Session) -> Optional[BeautifulSoup]:
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


# ─── Deduplication ────────────────────────────────────────────────────────────

def _dedup(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Set[tuple] = set()
    out: List[Dict[str, Any]] = []
    for r in records:
        key = (r.get("start_date"), r.get("home"), r.get("away"),
               r.get("market"), r.get("bet_team"), r.get("am_odds"))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ─── Nested file output helpers ───────────────────────────────────────────────

def _sport_from_url(url: str) -> str:
    """Extract sport slug from URL, e.g. '.../soccer/...' → 'soccer'."""
    path = url.replace(BASE_URL, "").strip("/")
    parts = path.split("/")
    return parts[2] if len(parts) >= 3 else "unknown"


def _to_slug(text: str) -> str:
    """Convert a league name to a safe filename slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_") or "unknown"


def _save_nested(records: List[Dict[str, Any]], base_dir: Path,
                 category: str, indent: Optional[int]) -> None:
    """
    Save records to:  base_dir / category / sport / league_slug.json
    Each record must have a temporary '_sport' key that is removed on write.
    """
    from collections import defaultdict
    grouped: Dict[str, Dict[str, List]] = defaultdict(lambda: defaultdict(list))

    for r in records:
        sport      = r.pop("_sport", "unknown")
        league_slug = _to_slug(r.get("league") or "unknown")
        grouped[sport][league_slug].append(r)

    total_files = 0
    for sport, leagues in sorted(grouped.items()):
        for league_slug, recs in sorted(leagues.items()):
            path = base_dir / category / sport / f"{league_slug}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(recs, indent=indent, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("Wrote %4d records → %s", len(recs), path)
            total_files += 1

    logger.info("Saved %d records across %d files under %s/%s/",
                len(records), total_files, base_dir, category)


# ─── Main scrape ──────────────────────────────────────────────────────────────

def scrape_live(delay: float = 0.8) -> List[Dict[str, Any]]:
    """
    Scrape all game-line pages and return only LIVE records
    (games whose start_date is in the past but still have valid odds).
    Each record includes a temporary '_sport' key for nested saving.
    """
    session = requests.Session()
    now_utc = datetime.now(tz=UTC_TZ).strftime("%Y-%m-%d %H:%M:%S")

    logger.info("Discovering game-line URLs…")
    urls = _discover_game_urls(session)
    logger.info("Found %d game-line URLs", len(urls))

    all_records: List[Dict[str, Any]] = []

    for i, (url, label) in enumerate(urls):
        logger.info("[%d/%d] %s", i + 1, len(urls), url)
        soup = _fetch(url, session)
        if soup is None:
            continue
        sport   = _sport_from_url(url)
        records = _parse_game_table(soup, url)
        for r in records:
            r["_sport"] = sport
        logger.info("           → %d records", len(records))
        all_records.extend(records)
        if i < len(urls) - 1:
            time.sleep(delay)

    all_records = _dedup(all_records)

    live = [
        r for r in all_records
        if r.get("start_date") and r["start_date"] < now_utc
    ]
    logger.info("Total unique records: %d  |  Live: %d", len(all_records), len(live))
    return live


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape BookMaker.eu LIVE game odds.",
        epilog="Output structure:  <output-dir>/live/<sport>/<league>.json",
    )
    parser.add_argument(
        "--output-dir", "-d", default=".", metavar="DIR",
        help="Base directory for output (default: current dir)",
    )
    parser.add_argument("--delay", type=float, default=0.8, metavar="SECS",
                        help="Seconds between requests (default: 0.8)")
    parser.add_argument("--indent", type=int, default=2, metavar="N",
                        help="JSON indent level (default: 2; 0 = compact)")
    parser.add_argument("--list-urls", action="store_true",
                        help="Print game-line URLs and exit")
    args = parser.parse_args()

    if args.list_urls:
        session = requests.Session()
        for url, label in _discover_game_urls(session):
            print(f"{url}  ({label})")
        return

    records = scrape_live(delay=args.delay)
    indent  = args.indent if args.indent > 0 else None
    _save_nested(records, Path(args.output_dir), "live", indent)


if __name__ == "__main__":
    main()
