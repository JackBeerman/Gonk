#!/usr/bin/env python3
"""List newly launched objects in orbit, grouped by day, from Space-Track.org.

Data source: the Space-Track SATCAT class (one row per cataloged object).
An object counts as "new in orbit" on the day of its LAUNCH date. By default
objects that have already decayed are excluded, so the list shows what is
still up there.

Credentials are read from the SPACETRACK_USER / SPACETRACK_PASS environment
variables, or prompted for interactively. They are never written to disk.

Examples:
    export SPACETRACK_USER='you@example.com'
    export SPACETRACK_PASS='...'
    python spacetrack_new.py                    # last 7 days
    python spacetrack_new.py --days 30 --payloads-only
    python spacetrack_new.py --format csv > new.csv
    python spacetrack_new.py --state seen.json  # only objects not seen last run

Notes on the API (https://www.space-track.org/documentation):
  * Auth is a session cookie from POST /ajaxauth/login.
  * Rate limits: 30 requests/minute and 300/hour. This script makes 3
    (login, one query, logout).
  * Newly launched objects often take days to weeks to be cataloged, so the
    most recent days will fill in over time. The --state option catches
    objects that appear in the catalog late.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import requests

BASE_URL = "https://www.space-track.org"
LOGIN_URL = f"{BASE_URL}/ajaxauth/login"
LOGOUT_URL = f"{BASE_URL}/ajaxauth/logout"
QUERY_URL = f"{BASE_URL}/basicspacedata/query"

FIELDS = [
    "NORAD_CAT_ID",
    "OBJECT_ID",
    "OBJECT_NAME",
    "OBJECT_TYPE",
    "COUNTRY",
    "LAUNCH",
    "SITE",
    "DECAY",
    "PERIOD",
    "INCLINATION",
    "APOGEE",
    "PERIGEE",
    "RCS_SIZE",
]


class SpaceTrackError(RuntimeError):
    pass


class SpaceTrackClient:
    def __init__(self, username: str, password: str, timeout: float = 60):
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "gonk-spacetrack-new/1.0"

    def __enter__(self) -> "SpaceTrackClient":
        self.login()
        return self

    def __exit__(self, *exc) -> None:
        self.logout()

    def login(self) -> None:
        resp = self.session.post(
            LOGIN_URL,
            data={"identity": self.username, "password": self.password},
            timeout=self.timeout,
        )
        # Space-Track returns 200 with a JSON error body on bad credentials.
        if resp.status_code != 200 or "Failed" in resp.text:
            raise SpaceTrackError(f"Login failed ({resp.status_code}): {resp.text[:200]}")
        if "chocolatechip" not in self.session.cookies:
            raise SpaceTrackError("Login did not return a session cookie; check credentials.")

    def logout(self) -> None:
        try:
            self.session.get(LOGOUT_URL, timeout=self.timeout)
        except requests.RequestException:
            pass
        finally:
            self.session.close()

    def query(self, path: str) -> list[dict]:
        url = f"{QUERY_URL}/{quote(path, safe='/,<>-=')}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 429:
            raise SpaceTrackError("Rate limited by Space-Track (30/min, 300/hr). Try again later.")
        if resp.status_code != 200:
            raise SpaceTrackError(f"Query failed ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        if isinstance(data, dict) and "error" in data:
            raise SpaceTrackError(f"Query error: {data['error']}")
        return data

    def recent_launches(
        self, days: int, payloads_only: bool = False, include_decayed: bool = False
    ) -> list[dict]:
        parts = [
            "class/satcat",
            f"LAUNCH/>now-{days}",
            "CURRENT/Y",
        ]
        if payloads_only:
            parts.append("OBJECT_TYPE/PAYLOAD")
        if not include_decayed:
            parts.append("DECAY/null-val")
        parts += [
            "orderby/LAUNCH desc,NORAD_CAT_ID asc",
            f"predicates/{','.join(FIELDS)}",
            "format/json",
        ]
        return self.query("/".join(parts))


def group_by_day(rows: Iterable[dict]) -> dict[str, list[dict]]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_day[row.get("LAUNCH") or "unknown"].append(row)
    for objs in by_day.values():
        objs.sort(key=lambda r: int(r.get("NORAD_CAT_ID") or 0))
    return dict(sorted(by_day.items(), reverse=True))


def filter_unseen(rows: list[dict], state_file: Path) -> list[dict]:
    """Return rows whose NORAD id was not in the state file, then update it."""
    seen: set[str] = set()
    if state_file.exists():
        seen = set(json.loads(state_file.read_text()).get("seen", []))
    new = [r for r in rows if str(r["NORAD_CAT_ID"]) not in seen]
    seen |= {str(r["NORAD_CAT_ID"]) for r in rows}
    state_file.write_text(json.dumps({"seen": sorted(seen, key=int)}, indent=0))
    return new


def print_text(by_day: dict[str, list[dict]]) -> None:
    if not by_day:
        print("No new objects found.")
        return
    total = sum(len(v) for v in by_day.values())
    print(f"{total} object(s) across {len(by_day)} launch day(s)\n")
    for day, objs in by_day.items():
        types = defaultdict(int)
        for o in objs:
            types[o.get("OBJECT_TYPE") or "?"] += 1
        summary = ", ".join(f"{n} {t.lower()}" for t, n in sorted(types.items()))
        print(f"== {day}  ({len(objs)}: {summary})")
        for o in objs:
            orbit = ""
            if o.get("PERIGEE") and o.get("APOGEE"):
                orbit = f"{o['PERIGEE']}x{o['APOGEE']} km @ {o.get('INCLINATION', '?')}°"
            print(
                f"   {o['NORAD_CAT_ID']:>6}  {o.get('OBJECT_ID', ''):<11} "
                f"{(o.get('OBJECT_NAME') or '')[:28]:<28} {(o.get('OBJECT_TYPE') or '')[:11]:<11} "
                f"{(o.get('COUNTRY') or ''):<6} {orbit}"
            )
        print()


def print_csv(rows: list[dict]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)


def get_credentials() -> tuple[str, str]:
    user = os.environ.get("SPACETRACK_USER") or input("Space-Track username: ")
    password = os.environ.get("SPACETRACK_PASS") or getpass.getpass("Space-Track password: ")
    return user, password


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=7, help="look back this many days (default 7)")
    p.add_argument("--payloads-only", action="store_true", help="exclude debris and rocket bodies")
    p.add_argument("--include-decayed", action="store_true", help="include objects already re-entered")
    p.add_argument("--state", type=Path, help="JSON file of seen NORAD ids; show only unseen objects")
    p.add_argument("--format", choices=["text", "json", "csv"], default="text")
    args = p.parse_args(argv)

    user, password = get_credentials()
    try:
        with SpaceTrackClient(user, password) as st:
            rows = st.recent_launches(args.days, args.payloads_only, args.include_decayed)
    except (SpaceTrackError, requests.RequestException) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.state:
        rows = filter_unseen(rows, args.state)

    if args.format == "json":
        json.dump(group_by_day(rows), sys.stdout, indent=2)
        print()
    elif args.format == "csv":
        print_csv(rows)
    else:
        print_text(group_by_day(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
