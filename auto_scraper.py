"""
BookMaker.eu Auto Scraper Orchestrator
=======================================
Launches and monitors all three scrapers concurrently 24/7:
  - live_scraper.py     (live in-game odds,     re-runs every  3 min)
  - pregame_scraper.py  (upcoming game odds,     re-runs every 15 min)
  - futures_scraper.py  (futures/outrights/props, re-runs every 60 min)

Each scraper runs once, exits, then the orchestrator waits the configured
interval before running it again. If a scraper crashes it is automatically
restarted after a shorter cool-down period.

Usage:
    python auto_scraper.py                # run all three scrapers
    python auto_scraper.py live           # run only the live scraper
    python auto_scraper.py pregame        # run only the pregame scraper
    python auto_scraper.py futures        # run only the futures scraper
    python auto_scraper.py live pregame   # run two scrapers
    python auto_scraper.py --help         # show this help

Press Ctrl+C to stop all scrapers gracefully.
"""

import subprocess
import sys
import os
import time
import signal
import threading
import logging
import random
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPTS: dict[str, dict] = {
    "live": {
        "script":           "live_scraper.py",
        "args":             ["--delay", "0.5"],
        "label":            "Live Scraper",
        "interval_min":     1,    # min seconds between successful runs
        "interval_max":     5,    # max seconds between successful runs
        "crash_delay_min":  1,
        "crash_delay_max":  5,
        "max_restarts":     0,    # 0 = unlimited
    },
    "pregame": {
        "script":           "pregame_scraper.py",
        "args":             ["--delay", "0.8"],
        "label":            "Pregame Scraper",
        "interval_min":     10,
        "interval_max":     15,
        "crash_delay_min":  10,
        "crash_delay_max":  15,
        "max_restarts":     0,
    },
    "futures": {
        "script":           "futures_scraper.py",
        "args":             ["--delay", "0.8"],
        "label":            "Futures Scraper",
        "interval_min":     50,
        "interval_max":     55,
        "crash_delay_min":  50,
        "crash_delay_max":  55,
        "max_restarts":     0,
    },
}

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE = LOGS_DIR / f"auto_scraper_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)),
    ],
)
logger = logging.getLogger("AutoScraper")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_stop_event = threading.Event()
_active_processes: dict[str, subprocess.Popen] = {}
_run_counts:       dict[str, int]               = {}   # how many times each ran
_last_run_time:    dict[str, float]             = {}   # monotonic time of last run
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def _log_path(name: str) -> Path:
    return LOGS_DIR / f"{name}_{datetime.now().strftime('%Y%m%d')}.log"


def _start_process(name: str, cfg: dict) -> tuple[subprocess.Popen, object]:
    """Spawn the scraper subprocess. Returns (proc, log_file_handle)."""
    script = BASE_DIR / cfg["script"]
    cmd    = [sys.executable, "-u", str(script)] + cfg["args"]

    log_fh = open(_log_path(name), "a", encoding="utf-8", buffering=1)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        stdout=log_fh,
        stderr=log_fh,
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )
    return proc, log_fh


def _terminate(name: str, proc: subprocess.Popen, label: str) -> None:
    """Gracefully terminate a child process."""
    if proc.poll() is not None:
        return
    logger.info(f"[{label}] Sending terminate signal (PID {proc.pid})…")
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            logger.warning(f"[{label}] Force-killing PID {proc.pid}")
            proc.kill()
    except OSError:
        pass
    logger.info(f"[{label}] Stopped.")


def _wait_interval(min_s: float, max_s: float, label: str, reason: str) -> None:
    """Sleep for a random duration in [min_s, max_s], waking early if stop is requested."""
    seconds  = random.uniform(min_s, max_s)
    deadline = time.monotonic() + seconds
    logger.info(f"[{label}] {reason} — next run in {_fmt_secs(seconds)}.")
    while time.monotonic() < deadline and not _stop_event.is_set():
        time.sleep(0.5)


def _fmt_secs(s: float) -> str:
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s) // 60}m {int(s) % 60}s"
    return f"{int(s) // 3600}h {(int(s) % 3600) // 60}m"


# ---------------------------------------------------------------------------
# Monitor thread — keeps one scraper alive on its own schedule
# ---------------------------------------------------------------------------

def monitor_scraper(name: str, cfg: dict) -> None:
    label            = cfg["label"]
    interval_min     = cfg["interval_min"]
    interval_max     = cfg["interval_max"]
    crash_delay_min  = cfg["crash_delay_min"]
    crash_delay_max  = cfg["crash_delay_max"]
    max_restarts     = cfg["max_restarts"]
    crash_count      = 0

    while not _stop_event.is_set():
        logger.info(f"[{label}] >> Starting run #{_run_counts.get(name, 0) + 1}...")
        run_start = time.monotonic()

        proc, log_fh = _start_process(name, cfg)
        with _lock:
            _active_processes[name] = proc

        # Wait for this run to complete (or stop signal)
        retcode = None
        while not _stop_event.is_set():
            try:
                retcode = proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                continue

        if _stop_event.is_set():
            _terminate(name, proc, label)
            try:
                log_fh.close()
            except OSError:
                pass
            return

        elapsed = time.monotonic() - run_start
        log_fh.close()

        with _lock:
            _run_counts[name] = _run_counts.get(name, 0) + 1
            _last_run_time[name] = time.monotonic()
            run_n = _run_counts[name]

        if retcode == 0:
            logger.info(
                f"[{label}] [OK] Run #{run_n} completed in {_fmt_secs(elapsed)}."
            )
            crash_count = 0  # reset crash streak on success
            _wait_interval(interval_min, interval_max, label, "Completed successfully")
        else:
            crash_count += 1
            logger.warning(
                f"[{label}] [FAIL] Run #{run_n} exited with code {retcode} "
                f"after {_fmt_secs(elapsed)}. Crash #{crash_count}"
                + (f"/{max_restarts}" if max_restarts else "")
            )
            if max_restarts and crash_count >= max_restarts:
                logger.error(f"[{label}] Reached max crash limit ({max_restarts}). Stopping.")
                return
            _wait_interval(crash_delay_min, crash_delay_max, label, f"Crashed (code {retcode}), cooling down")

    logger.info(f"[{label}] Monitor thread exiting.")


# ---------------------------------------------------------------------------
# Signal handler
# ---------------------------------------------------------------------------

def _handle_stop(signum, frame):  # noqa: ARG001
    if not _stop_event.is_set():
        logger.info("\n>>> Stop signal received - shutting down all scrapers...")
        _stop_event.set()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    requested: set[str] = set()
    for arg in sys.argv[1:]:
        if arg in ("--help", "-h"):
            print(__doc__)
            return
        if arg in SCRIPTS:
            requested.add(arg)
        else:
            logger.error(f"Unknown argument: '{arg}'. Valid options: {list(SCRIPTS.keys())}")
            sys.exit(1)

    targets = {k: v for k, v in SCRIPTS.items() if not requested or k in requested}

    if not targets:
        logger.error("No valid scraper targets selected.")
        sys.exit(1)

    signal.signal(signal.SIGINT,  _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    logger.info("=" * 65)
    logger.info("  BookMaker.eu Auto Scraper Orchestrator")
    logger.info(f"  Scrapers : {', '.join(targets.keys())}")
    logger.info("  Schedule :")
    for name, cfg in targets.items():
        logger.info(
            f"    {cfg['label']:<20} "
            f"every {_fmt_secs(cfg['interval_min'])}–{_fmt_secs(cfg['interval_max'])}"
            f"  (crash cool-down: {_fmt_secs(cfg['crash_delay_min'])}–{_fmt_secs(cfg['crash_delay_max'])})"
        )
    logger.info(f"  Log dir  : {LOGS_DIR}")
    logger.info("  Press Ctrl+C to stop.")
    logger.info("=" * 65)

    threads: list[threading.Thread] = []
    for name, cfg in targets.items():
        t = threading.Thread(
            target=monitor_scraper,
            args=(name, cfg),
            daemon=True,
            name=f"monitor-{name}",
        )
        t.start()
        threads.append(t)
        time.sleep(2)  # small stagger so console output doesn't collide

    # Heartbeat — print status every 5 minutes
    last_heartbeat = time.monotonic()
    try:
        while not _stop_event.is_set():
            time.sleep(1)
            if time.monotonic() - last_heartbeat >= 300:
                last_heartbeat = time.monotonic()
                with _lock:
                    alive  = {n: p for n, p in _active_processes.items() if p.poll() is None}
                    counts = dict(_run_counts)
                status_parts = []
                for name, cfg in targets.items():
                    pid    = _active_processes.get(name)
                    runs   = counts.get(name, 0)
                    last_t = _last_run_time.get(name)
                    ago    = f"{_fmt_secs(time.monotonic() - last_t)} ago" if last_t else "never"
                    is_running = pid is not None and pid.poll() is None
                    state  = f"PID {pid.pid}" if is_running else "waiting"
                    status_parts.append(f"{cfg['label']}: runs={runs}, last={ago}, {state}")
                logger.info("[Heartbeat] " + " | ".join(status_parts))
    except KeyboardInterrupt:
        _stop_event.set()

    logger.info(">>> Waiting for all scrapers to finish…")
    for t in threads:
        t.join(timeout=30)
    logger.info(">>> All scrapers stopped. Goodbye!")


if __name__ == "__main__":
    main()
