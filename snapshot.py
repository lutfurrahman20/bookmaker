"""
BookMaker.eu Daily Snapshot
============================
Copies today's live / pregame / futures data into a dated snapshot folder:

    snapshots/YYYY-MM-DD/
        pregame/
            soccer/
                england_premier_league.json
            football/
                nfl.json
            ...
        live/
            baseball/
                mlb.json
            ...
        futures/
            football/
                nfl_futures.json
            ...

Run manually:
    python snapshot.py                   # snapshot today's date
    python snapshot.py --date 2026-07-29 # snapshot a specific date
    python snapshot.py --list            # list all saved snapshots

Schedule via Windows Task Scheduler to run daily at midnight:
    Program : python
    Arguments: F:\\bookmaker\\snapshot.py
    Start in : F:\\bookmaker

The snapshot is a point-in-time copy — it never overwrites an existing
snapshot for the same date unless you pass --force.
"""

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(
            stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
        )
    ],
)
logger = logging.getLogger("Snapshot")

BASE_DIR      = Path(__file__).parent
SNAPSHOTS_DIR = BASE_DIR / "snapshots"

CATEGORIES = {
    "live":    BASE_DIR / "live",
    "pregame": BASE_DIR / "pregame",
    "futures": BASE_DIR / "futures",
}


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _count_files(directory: Path) -> int:
    return sum(1 for _ in directory.rglob("*.json")) if directory.exists() else 0


def _count_records(directory: Path) -> int:
    total = 0
    if not directory.exists():
        return 0
    for jf in directory.rglob("*.json"):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            if isinstance(data, list):
                total += len(data)
        except Exception:
            pass
    return total


def take_snapshot(date: str, force: bool = False) -> dict:
    """
    Copy CATEGORIES into snapshots/{date}/{category}/{sport}/{league}.json.
    Returns a summary dict.
    """
    snap_dir = SNAPSHOTS_DIR / date
    snap_dir.mkdir(parents=True, exist_ok=True)

    summary = {"date": date, "taken_at": datetime.now(tz=timezone.utc).isoformat(), "categories": {}}

    for category, src_dir in CATEGORIES.items():
        cat_snap = snap_dir / category

        if not src_dir.exists():
            logger.warning("[%s] Source directory not found: %s — skipping.", category, src_dir)
            summary["categories"][category] = {"status": "skipped", "reason": "source not found"}
            continue

        if cat_snap.exists() and not force:
            existing = _count_files(cat_snap)
            logger.info("[%s] Snapshot already exists (%d files). Use --force to overwrite.", category, existing)
            summary["categories"][category] = {"status": "skipped", "reason": "already exists", "files": existing}
            continue

        if cat_snap.exists() and force:
            shutil.rmtree(cat_snap)

        # Copy entire sport/league tree
        shutil.copytree(src_dir, cat_snap)

        files   = _count_files(cat_snap)
        records = _count_records(cat_snap)
        logger.info("[%s] Saved %d files / %d records -> %s", category, files, records, cat_snap)
        summary["categories"][category] = {"status": "saved", "files": files, "records": records}

    # Write a metadata file
    meta_path = snap_dir / "_meta.json"
    meta_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Snapshot complete -> %s", snap_dir)
    return summary


def list_snapshots() -> None:
    """Print all available snapshots with record counts."""
    if not SNAPSHOTS_DIR.exists() or not any(SNAPSHOTS_DIR.iterdir()):
        print("No snapshots found.")
        return

    dates = sorted(
        [d.name for d in SNAPSHOTS_DIR.iterdir() if d.is_dir()],
        reverse=True,
    )
    print(f"{'Date':<14} {'Pregame':>10} {'Live':>8} {'Futures':>10}  Path")
    print("-" * 65)
    for date in dates:
        snap_dir = SNAPSHOTS_DIR / date
        pg = _count_records(snap_dir / "pregame")
        lv = _count_records(snap_dir / "live")
        ft = _count_records(snap_dir / "futures")
        print(f"{date:<14} {pg:>10,} {lv:>8,} {ft:>10,}  {snap_dir}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Take a daily snapshot of BookMaker.eu scraped data.",
        epilog="Schedule this script to run daily via Windows Task Scheduler.",
    )
    parser.add_argument(
        "--date", default=_today(), metavar="YYYY-MM-DD",
        help="Date to snapshot (default: today UTC)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing snapshot for the same date",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List all available snapshots and exit",
    )
    args = parser.parse_args()

    if args.list:
        list_snapshots()
        return

    logger.info("Taking snapshot for date: %s", args.date)
    summary = take_snapshot(args.date, force=args.force)

    print("\nSummary:")
    for cat, info in summary["categories"].items():
        status = info.get("status", "?")
        if status == "saved":
            print(f"  {cat:<10} {info['files']:>4} files  {info['records']:>7,} records")
        else:
            print(f"  {cat:<10} {status} ({info.get('reason', '')})")


if __name__ == "__main__":
    main()
