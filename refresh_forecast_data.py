"""
Hourly data refresh for the OMP Purchasing Dashboard.

Runs each forecast script in sequence so that flooringwebappJSON,
sundrieswebappJSON, and mouldingwebappJSON are always up-to-date.

Run once manually to verify, then register with setup_scheduled_task.ps1
to have Windows Task Scheduler call it every hour automatically.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR    = Path(__file__).resolve().parent
LOG_FILE    = BASE_DIR / "refresh_log.txt"
STATUS_FILE = BASE_DIR / "_refresh_status.json"

# Scripts to run in order.  Flooring runs this file itself in CLI mode;
# sundries and moulding are separate scripts.
SCRIPTS: list[tuple[str, Path]] = [
    ("Flooring", BASE_DIR / "flooringwebapp.py"),
    ("Sundries", BASE_DIR / "sundrieswebapp_refactored.py"),
    ("Moulding", BASE_DIR / "mouldingwebapp_refactored.py"),
]

TIMEOUT_SECONDS = 2700  # 45 minutes per script


def _setup_logging() -> None:
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Also echo to stdout so the Task Scheduler log captures it.
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))


def _run_script(name: str, path: Path) -> bool:
    if not path.exists():
        logging.error("%s: script not found at %s", name, path)
        return False

    logging.info("%s: starting", name)
    start = datetime.now()

    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        elapsed = (datetime.now() - start).total_seconds()

        if result.returncode == 0:
            logging.info("%s: completed in %.0fs", name, elapsed)
            return True

        logging.error("%s: exited with code %d after %.0fs", name, result.returncode, elapsed)
        if result.stderr:
            logging.error("%s: stderr: %s", name, result.stderr[-800:])
        return False

    except subprocess.TimeoutExpired:
        logging.error("%s: timed out after %ds", name, TIMEOUT_SECONDS)
        return False
    except Exception as exc:
        logging.error("%s: unexpected error: %s", name, exc)
        return False


def main() -> None:
    _setup_logging()
    logging.info("=" * 60)
    logging.info("OMP Dashboard hourly refresh started")

    results: dict[str, bool] = {}
    for name, script in SCRIPTS:
        results[name] = _run_script(name, script)

    status = {
        "last_run":  datetime.now().isoformat(),
        "results":   results,
        "all_ok":    all(results.values()),
    }
    try:
        STATUS_FILE.write_text(json.dumps(status, indent=2), encoding="utf-8")
    except Exception as exc:
        logging.error("Could not write status file: %s", exc)

    ok  = sum(results.values())
    tot = len(results)
    logging.info("Refresh complete: %d/%d scripts succeeded", ok, tot)
    if ok < tot:
        failed = [n for n, ok_ in results.items() if not ok_]
        logging.warning("Failed: %s — check refresh_log.txt for details", ", ".join(failed))
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
