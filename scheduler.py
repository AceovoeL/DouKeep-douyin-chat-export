#!/usr/bin/env python3
"""Scheduled scraper using APScheduler. Reads config from environment variables."""
import os
import subprocess
import sys
import signal

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger


def run_scrape():
    """Execute a single scrape run."""
    cmd = [sys.executable, "-u", "extract.py"]
    if os.environ.get("SCRAPER_INCREMENTAL", "true").lower() == "true":
        cmd.append("--incremental")
    filt = os.environ.get("SCRAPER_FILTER", "")
    if filt:
        cmd.extend(["--filter", filt])

    print(f"[scheduler] Starting scrape: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, env=os.environ)
    print(f"[scheduler] Scrape finished with exit code {result.returncode}", flush=True)


def main():
    cron_expr = os.environ.get("SCRAPER_SCHEDULE", "").strip()
    if not cron_expr:
        print("[scheduler] No SCRAPER_SCHEDULE set, running once and exiting", flush=True)
        run_scrape()
        return

    parts = cron_expr.split()
    if len(parts) != 5:
        print(f"[scheduler] Invalid cron expression: {cron_expr} (expected 5 fields)", flush=True)
        sys.exit(1)

    trigger = CronTrigger(
        minute=parts[0],
        hour=parts[1],
        day=parts[2],
        month=parts[3],
        day_of_week=parts[4],
    )

    scheduler = BlockingScheduler()
    scheduler.add_job(run_scrape, trigger, id="scrape", max_instances=1)

    # Run once immediately
    print(f"[scheduler] Schedule: {cron_expr}", flush=True)
    print("[scheduler] Running initial scrape...", flush=True)
    run_scrape()

    print("[scheduler] Waiting for next scheduled run...", flush=True)

    def shutdown(signum, frame):
        print("[scheduler] Shutting down...", flush=True)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    scheduler.start()


if __name__ == "__main__":
    main()
