"""Command line: gonk {setup,rules,run,baseline,daily,status} (or python -m gonk ...)"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from . import emailer, export, pipeline, report
from .spacetrack import REQUEST_LIMITS, RULES, Ledger, PolicyError, SpaceTrack, SpaceTrackError, utcnow
from .store import Store

log = logging.getLogger("gonk")


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; one pair of surrounding quotes is removed."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        out[key.strip()] = val
    return out


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load .env into the environment. Real environment variables win."""
    for key, val in read_env_file(path).items():
        os.environ.setdefault(key, val)


def write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write .env readable only by the current user (mode 600)."""
    lines = ["# Gonk settings, written by 'gonk setup'. Keep this file private; it is git-ignored."]
    lines += [f"{k}='{v}'" for k, v in values.items() if v]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


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
    files = export.export(res, Path(args.data_dir))
    print("CSV data: " + ", ".join(str(f) for f in files))
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


def print_rules(store: Store) -> None:
    now = utcnow()
    ledger = Ledger(store.ledger_path)
    print("Space-Track API calls Gonk can make (all times UTC)\n")
    print("Account-wide request limits")
    print(f"  {'':<28} {'Space-Track rule':<18} Gonk applies")
    for name, published, ours in REQUEST_LIMITS:
        print(f"  {name:<28} {published:<18} {ours}")
    print("\nData classes (Gonk refuses any class not listed here)")
    print(f"  {'Class':<14} {'Limit':<31} {'Last queried':<18} {'Next allowed':<18} Used for")
    for cls, rule in RULES.items():
        last = ledger.last.get(cls)
        last_txt = last[:16].replace("T", " ") if last else "never"
        nxt = ledger.next_allowed(cls, now)
        nxt_txt = "now" if nxt <= now else f"{nxt:%Y-%m-%d %H:%M}"
        print(f"  {cls:<14} {rule.limit:<31} {last_txt:<18} {nxt_txt:<18} {rule.used_for}")
    print("\nA normal daily run makes about 6-8 requests. Login and logout also count as requests.")
    print("--force skips the per-class checks; repeated queries can get the account suspended.")
    print("Rules: https://www.space-track.org/documentation#/api (API Use Guidelines)")


def cmd_rules(args, store: Store) -> None:
    print_rules(store)


def cmd_setup(args, store: Store) -> None:
    env_path = Path(args.env_file)
    values = read_env_file(env_path)
    print("Gonk setup. Your answers are saved to", env_path.resolve())
    print("That file stays on this computer, is readable only by you, and is never committed.")
    print("Press Enter to keep a value shown in [brackets].\n")

    def ask(key: str, label: str, default: str = "", secret: bool = False) -> None:
        current = values.get(key) or default
        if secret:
            shown = " [keep current]" if values.get(key) else ""
            answer = getpass.getpass(f"{label}{shown}: ")
        else:
            answer = input(f"{label}{f' [{current}]' if current else ''}: ").strip()
        values[key] = answer or current

    print("Space-Track.org account (free: https://www.space-track.org/auth/createAccount)")
    ask("SPACETRACK_USER", "  Username (email)")
    ask("SPACETRACK_PASS", "  Password (hidden)", secret=True)
    if not values.get("SPACETRACK_USER") or not values.get("SPACETRACK_PASS"):
        raise SystemExit("A Space-Track username and password are required.")
    if not args.no_verify:
        print("  Checking the login with Space-Track (one login and logout, no data queries)...")
        try:
            SpaceTrack(values["SPACETRACK_USER"], values["SPACETRACK_PASS"],
                       Ledger(store.ledger_path)).verify_login()
        except SpaceTrackError as e:
            raise SystemExit(f"  Login failed, nothing saved: {e}")
        print("  Login OK.")

    if input("\nSet up email delivery now? [y/N]: ").strip().lower().startswith("y"):
        print("Gmail: use smtp.gmail.com, port 587 and an App Password "
              "(https://myaccount.google.com/apppasswords).")
        ask("SMTP_HOST", "  SMTP server", "smtp.gmail.com")
        ask("SMTP_PORT", "  SMTP port", "587")
        ask("SMTP_USER", "  SMTP username", values.get("SPACETRACK_USER", ""))
        ask("SMTP_PASSWORD", "  SMTP password (hidden)", secret=True)
        ask("EMAIL_FROM", "  Send from", values.get("SMTP_USER", ""))
        ask("EMAIL_TO", "  Send to (comma-separated)", values.get("EMAIL_FROM", ""))

    write_env_file(env_path, values)
    print(f"\nSaved {env_path}. Next:")
    print("  gonk rules     see which API calls are allowed right now")
    print("  gonk run       first run takes the baseline (after 17:00 UTC); later runs send the daily report")


def cmd_status(args, store: Store) -> None:
    if not store.has_baseline():
        print("No baseline yet.")
        return
    meta, objects = store.load_catalog()
    print(f"Objects in orbit (snapshot): {len(objects):,}")
    for k, v in meta.items():
        print(f"{k}: {len(v)} recently removed ids" if k == "departed" else f"{k}: {v}")
    print()
    print_rules(store)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="gonk", description="Daily report of objects entering and leaving orbit, from Space-Track.org.")
    p.add_argument("--state-dir", default=os.environ.get("GONK_STATE_DIR", "state"),
                   help="where the snapshot, history and query ledger live (default ./state)")
    p.add_argument("--out-dir", default=os.environ.get("GONK_OUT_DIR", "reports"),
                   help="where reports are written (default ./reports)")
    p.add_argument("--data-dir", default=os.environ.get("GONK_DATA_DIR", "data"),
                   help="where the CSV dataset is kept: baseline, one file per day, running totals (default ./data)")
    p.add_argument("--no-email", action="store_true", help="write the report but do not email it")
    p.add_argument("--force", action="store_true",
                   help="skip the once-per-day / after-17:00-UTC checks (use sparingly: repeated "
                        "queries can get your Space-Track account suspended)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("setup", help="enter your Space-Track login (and optional email settings) into .env")
    st.add_argument("--env-file", default=".env", help="where to save settings (default ./.env)")
    st.add_argument("--no-verify", action="store_true", help="save without test-logging in to Space-Track")
    sub.add_parser("rules", help="show the Space-Track API calls Gonk can make and when each is next allowed")
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
        {"setup": cmd_setup, "rules": cmd_rules, "run": cmd_run, "baseline": cmd_baseline,
         "daily": cmd_daily, "status": cmd_status}[args.cmd](args, store)
    except PolicyError as e:
        print(f"Not querying Space-Track: {e}", file=sys.stderr)
        return 2
    except (SpaceTrackError, emailer.EmailConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
