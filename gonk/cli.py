"""Command line: python -m gonk {run,baseline,daily,status}"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import emailer, pipeline, report
from .spacetrack import Ledger, PolicyError, SpaceTrack, SpaceTrackError, utcnow
from .store import Store

log = logging.getLogger("gonk")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (KEY=VALUE lines). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


def client(store: Store, force: bool) -> SpaceTrack:
    user, pw = os.environ.get("SPACETRACK_USER"), os.environ.get("SPACETRACK_PASS")
    if not user or not pw:
        raise SpaceTrackError("Set SPACETRACK_USER and SPACETRACK_PASS (environment or .env file).")
    return SpaceTrack(user, pw, Ledger(store.ledger_path), force=force)


def write_outputs(rep: report.Report, out_dir: Path, res: pipeline.RunResult) -> Path:
    folder = out_dir / f"{res.run_at:%Y-%m-%d}-{res.kind}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.html").write_text(rep.html, encoding="utf-8")
    (folder / "summary.txt").write_text(rep.subject + "\n\n" + rep.text, encoding="utf-8")
    for name, content in rep.attachments.items():
        if isinstance(content, bytes):
            (folder / name).write_bytes(content)
        else:
            (folder / name).write_text(content, encoding="utf-8")
    (out_dir / "latest.html").write_text(rep.html, encoding="utf-8")
    return folder


def deliver(rep: report.Report, res: pipeline.RunResult, args) -> None:
    folder = write_outputs(rep, Path(args.out_dir), res)
    print(f"Report written to {folder}/report.html")
    print(rep.subject)
    if args.no_email:
        return
    if not emailer.configured():
        print("Email not configured (set SMTP_HOST, EMAIL_TO, SMTP_USER, SMTP_PASSWORD) - skipped sending.")
        return
    sent_to = emailer.send(rep)
    print(f"Emailed to {', '.join(sent_to)}")


def cmd_baseline(args, store: Store) -> None:
    if store.has_baseline() and not args.reset:
        raise SystemExit("A baseline already exists. Use 'daily', or 'baseline --reset' to start over.")
    now = utcnow()
    if args.from_json:
        rows = json.loads(Path(args.from_json).read_text())
        # Reference tables still come from Space-Track if credentials are available.
        st = client(store, args.force) if os.environ.get("SPACETRACK_USER") else None
        try:
            res = pipeline.run_baseline(st, store, now, rows=rows)
        finally:
            if st:
                st.close()
    else:
        with client(store, args.force) as st:
            res = pipeline.run_baseline(st, store, now)
    deliver(report.build(res), res, args)


def cmd_daily(args, store: Store) -> None:
    if not store.has_baseline():
        raise SystemExit("No baseline yet. Run 'baseline' (or 'run') first.")
    with client(store, args.force) as st:
        res = pipeline.run_daily(st, store, utcnow(), force_full=args.full)
        log.info("Space-Track requests this run: %d", st.requests_made)
    deliver(report.build(res), res, args)


def cmd_run(args, store: Store) -> None:
    if store.has_baseline():
        cmd_daily(args, store)
    else:
        args.reset, args.from_json = False, None
        cmd_baseline(args, store)


def cmd_status(args, store: Store) -> None:
    if not store.has_baseline():
        print("No baseline yet.")
        return
    meta, objects = store.load_catalog()
    print(f"Objects in orbit (snapshot): {len(objects):,}")
    for k, v in meta.items():
        print(f"{k}: {v}")
    ledger = Ledger(store.ledger_path).last
    if ledger:
        print("Last Space-Track query per class:")
        for k, v in sorted(ledger.items()):
            print(f"  {k:<14} {v}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="gonk", description="Daily report of objects entering and leaving orbit, from Space-Track.org.")
    p.add_argument("--state-dir", default=os.environ.get("GONK_STATE_DIR", "state"),
                   help="where the snapshot, history and query ledger live (default ./state)")
    p.add_argument("--out-dir", default=os.environ.get("GONK_OUT_DIR", "reports"),
                   help="where reports are written (default ./reports)")
    p.add_argument("--no-email", action="store_true", help="write the report but do not email it")
    p.add_argument("--force", action="store_true",
                   help="skip the once-per-day / after-17:00-UTC checks (use sparingly: repeated "
                        "queries can get your Space-Track account suspended)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="baseline on first use, daily report afterwards (use this in cron)")
    b = sub.add_parser("baseline", help="download the full on-orbit catalog as the starting point")
    b.add_argument("--reset", action="store_true", help="replace an existing baseline")
    b.add_argument("--from-json", metavar="FILE", help="import a SATCAT JSON export instead of downloading")
    d = sub.add_parser("daily", help="report objects added/removed since the last run")
    d.add_argument("--full", action="store_true", help="also re-download the full catalog and diff it")
    sub.add_parser("status", help="show snapshot and query-ledger state")
    args = p.parse_args(argv)
    args.full = getattr(args, "full", False)
    args.reset = getattr(args, "reset", False)
    args.from_json = getattr(args, "from_json", None)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    store = Store(Path(args.state_dir))
    try:
        {"run": cmd_run, "baseline": cmd_baseline, "daily": cmd_daily, "status": cmd_status}[args.cmd](args, store)
    except PolicyError as e:
        print(f"Not querying Space-Track: {e}", file=sys.stderr)
        return 2
    except (SpaceTrackError, emailer.EmailConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
