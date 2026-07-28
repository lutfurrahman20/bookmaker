#!/usr/bin/env python3
"""
BookMaker.eu FUTURES odds scraper
Scrapes all futures / outrights / props pages including:
  - Season winner futures  (NFL, MLB, NBA, NHL, NCAA, Soccer…)
  - Player/team awards     (NFL Awards, MLB Awards, WNBA Awards…)
  - Season win totals      (NFL Regular Season Wins, NCAA Season Wins…)
  - Make-the-playoffs      (NFL, NBA, MLB Make THE Playoffs…)
  - Stat leaders           (MLB Stat Leaders, RBI, HR, SO…)
  - Tournament winners     (PGA, WTA, ATP Winner…)
  - Special props          (Entertainment, Politics, Motorsport…)

Usage:
    python futures_scraper.py                  # print JSON to stdout
    python futures_scraper.py -o futures.json  # save to file
    python futures_scraper.py --delay 0.5      # faster requests
    python futures_scraper.py --indent 0       # compact JSON
    python futures_scraper.py --list-urls      # preview URLs
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

# URL slug keywords that mark a futures/props page
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


# ─── Date / time parsers ──────────────────────────────────────────────────────

# Matches "09/10 17:20" from <span class="oddsFecha">
_FECHA_RE = re.compile(r"(\d{1,2})/(\d{2})\s+(\d{1,2}:\d{2})")

# Matches "07/28\n12:07" style from Yes/No time cells
_YESNO_DATE_RE = re.compile(r"(\d{1,2})/(\d{2})\s+(\d{1,2}:\d{2})")


def _to_utc(year: int, month: int, day: int, tpart: str, fmt: str) -> Optional[str]:
    now = datetime.now(tz=PT_TZ)
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


def _parse_fecha(text: str) -> Optional[str]:
    m = _FECHA_RE.search(text)
    if not m:
        return None
    return _to_utc(datetime.now(tz=PT_TZ).year,
                   int(m.group(1)), int(m.group(2)),
                   m.group(3), "%Y-%m-%d %H:%M")


# ─── URL discovery (futures pages only) ──────────────────────────────────────

def _discover_futures_urls(session: requests.Session) -> List[Tuple[str, str]]:
    """Return (url, label) for all futures/props pages in the navigation."""
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
        if len(parts) <= 3:
            continue
        if path in seen:
            continue
        slug = path.rstrip("/").split("/")[-1]
        if not any(kw in slug for kw in _FUTURES_KEYWORDS):
            continue              # skip game-line pages
        seen.add(path)
        results.append((f"{BASE_URL}{path}", a.get_text(strip=True)))

    return results


# ─── HTML helpers ─────────────────────────────────────────────────────────────

def _tag_text(tag: Optional[Tag]) -> str:
    return tag.get_text(strip=True) if tag else ""


# ─── Futures base record ──────────────────────────────────────────────────────

def _base(league: str, tournament: str, start_date: Optional[str]) -> Dict[str, Any]:
    return {
        "limit": None, "tournament": tournament, "betlink": None,
        "sgp": None, "league": league, "bet_occurence": None,
        "start_date": start_date, "home_short": None,
        "internal_betlink": None, "away_brief": None,
        "bet_player": None, "home": None, "home_brief": None,
        "is_main": True, "away": None, "away_short": None,
        "book": BOOK_NAME,
    }


# ─── Standard futures parser ──────────────────────────────────────────────────
# Handles pages like NFL Futures, MLB Futures, NBA Awards, PGA Winner, etc.
# Table structure:
#   <th class="oddsTitle">
#       <span class="oddsFecha">MM/DD HH:MM</span><br/>
#       LEAGUE NAME<br/>
#       BET DESCRIPTION<br/>
#       <span class="QST">SEASON INFO</span>
#   </th>
#   <td class="odds SP">Team Name +Odds</td>  ← one row per selection

_TEAM_ODDS_RE = re.compile(r"^(.+?)\s+([+\-]\d[\d,]*)$")


def _parse_standard_futures(table: Tag) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    cur_league     = ""
    cur_tournament = ""
    cur_date: Optional[str] = None

    for row in table.find_all("tr"):
        header = row.find("th", class_="oddsTitle")
        if header:
            fecha_span = header.find("span", class_="oddsFecha")
            if fecha_span:
                cur_date = _parse_fecha(fecha_span.get_text(strip=True))
                fecha_span.decompose()

            parts: List[str] = []
            buf: List[str] = []
            for node in header.children:
                if isinstance(node, NavigableString):
                    t = str(node).strip()
                    if t:
                        buf.append(t)
                elif isinstance(node, Tag):
                    if node.name == "br":
                        s = " ".join(buf).strip()
                        if s:
                            parts.append(s)
                        buf = []
                    else:
                        buf.append(node.get_text(strip=True))
            s = " ".join(buf).strip()
            if s:
                parts.append(s)

            cur_league     = parts[0] if parts else ""
            cur_tournament = " ".join(parts[1:]) if len(parts) > 1 else cur_league
            continue

        sp_td = row.find("td", class_=lambda c: c and "SP" in c.split())
        if sp_td:
            raw = sp_td.get_text(strip=True)
            m = _TEAM_ODDS_RE.match(raw)
            if m:
                odds = _parse_odds(m.group(2))
                if odds is not None:
                    records.append({
                        **_base(cur_league, cur_tournament, cur_date),
                        "am_odds": odds,
                        "bet_team": m.group(1).strip(),
                        "market": "Futures",
                        "line": None,
                    })

    return records


# ─── Yes/No futures parser ────────────────────────────────────────────────────
# Handles pages like MLB Futures YES/NO, NFL Futures YES/NO, etc.
# Table structure (one section per bet):
#   <th class="oddsTitle" colspan="3">LEAGUE YES/NO Bet Description</th>
#   <td class="odds TM …" rowspan="2">MM/DD\nHH:MM</td>
#   <td class="odds HV …">Yes</td>   <td class="odds PR …">+Odds</td>
#   <td class="odds HV …">No</td>    <td class="odds PR …">-Odds</td>

def _parse_yesno_futures(table: Tag) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    cur_league     = ""
    cur_tournament = ""
    cur_date: Optional[str] = None

    for row in table.find_all("tr"):
        header = row.find("th", class_="oddsTitle")
        if header:
            full = header.get_text(strip=True)
            cur_league     = full
            cur_tournament = full
            cur_date       = None
            continue

        cells = row.find_all("td")
        if not cells:
            continue

        time_td = next((c for c in cells if "TM" in (c.get("class") or [])), None)
        if time_td:
            cur_date = _parse_fecha(time_td.get_text(strip=True).replace("\n", " "))

        hv_td = next((c for c in cells if "HV" in (c.get("class") or [])), None)
        pr_td = next((c for c in cells if "PR" in (c.get("class") or [])), None)
        option = _tag_text(hv_td)

        if pr_td and option:
            odds = _parse_odds(_tag_text(pr_td))
            if odds is not None:
                records.append({
                    **_base(cur_league, cur_tournament, cur_date),
                    "am_odds": odds,
                    "bet_team": option,
                    "market": "Futures",
                    "line": None,
                })

    return records


# ─── Page parser (auto-detects table type) ────────────────────────────────────

def _parse_futures_page(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for table in soup.find_all("table"):
        yesno_hdr = table.find("th", attrs={"class": "oddsTitle", "colspan": "3"})
        if yesno_hdr:
            records.extend(_parse_yesno_futures(table))
        else:
            records.extend(_parse_standard_futures(table))
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
        key = (r.get("start_date"), r.get("tournament"),
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
        sport       = r.pop("_sport", "unknown")
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

def scrape_futures(delay: float = 0.8) -> List[Dict[str, Any]]:
    """
    Scrape all futures/props pages and return structured records.
    Each record includes a temporary '_sport' key for nested saving.
    """
    session = requests.Session()

    logger.info("Discovering futures URLs…")
    urls = _discover_futures_urls(session)
    logger.info("Found %d futures URLs", len(urls))

    all_records: List[Dict[str, Any]] = []

    for i, (url, label) in enumerate(urls):
        logger.info("[%d/%d] %s", i + 1, len(urls), url)
        soup = _fetch(url, session)
        if soup is None:
            continue
        sport   = _sport_from_url(url)
        records = _parse_futures_page(soup)
        for r in records:
            r["_sport"] = sport
        logger.info("           → %d records", len(records))
        all_records.extend(records)
        if i < len(urls) - 1:
            time.sleep(delay)

    all_records = _dedup(all_records)
    logger.info("Total unique futures records: %d", len(all_records))
    return all_records


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape BookMaker.eu FUTURES odds.",
        epilog="Output structure:  <output-dir>/futures/<sport>/<league>.json",
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
                        help="Print futures URLs and exit")
    args = parser.parse_args()

    if args.list_urls:
        session = requests.Session()
        for url, label in _discover_futures_urls(session):
            print(f"{url}  ({label})")
        return

    records = scrape_futures(delay=args.delay)
    indent  = args.indent if args.indent > 0 else None
    _save_nested(records, Path(args.output_dir), "futures", indent)


if __name__ == "__main__":
    main()
