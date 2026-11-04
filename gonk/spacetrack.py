"""Space-Track.org API client that enforces the published API use guidelines.

Rules implemented (https://www.space-track.org/documentation#/api, "API Use Guidelines"):

* Fewer than 30 requests/minute and 300 requests/hour. We stay well below:
  at most 20/minute, 200/hour, and at least 2.5 s between requests.
* Per-class retrieval frequency, tracked in a persistent ledger so the limits
  hold across separate runs of the program:
    - satcat, satcat_debut, satcat_change, decay, boxscore:
      once per UTC day, and only after 17:00 UTC
    - gp: once per hour
    - launch_site: no published limit; we cache it and refresh at most daily
* Bulk queries: many objects are requested in one call using comma-delimited
  NORAD id lists rather than one request per satellite.
* The account is logged out at the end of every session.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://www.space-track.org"
USER_AGENT = "gonk-orbit-report/1.0 (+https://github.com/JackBeerman/Gonk)"

# Max comma-delimited ids per request; keeps URLs well under common length limits.
ID_CHUNK = 250


class SpaceTrackError(RuntimeError):
    """The API returned an error or could not be reached."""


class PolicyError(RuntimeError):
    """A query would break the Space-Track API use guidelines."""


@dataclass(frozen=True)
class Rule:
    daily_after_1700: bool = False      # once per UTC day, only after 17:00 UTC
    min_interval: timedelta | None = None


RULES: dict[str, Rule] = {
    "satcat": Rule(daily_after_1700=True),
    "satcat_debut": Rule(daily_after_1700=True),
    "satcat_change": Rule(daily_after_1700=True),
    "decay": Rule(daily_after_1700=True),
    "boxscore": Rule(daily_after_1700=True),
    "gp": Rule(min_interval=timedelta(hours=1)),
    "launch_site": Rule(min_interval=timedelta(hours=24)),
}


class Ledger:
    """Persistent record of when each API class was last queried."""

    def __init__(self, path: Path):
        self.path = path
        self.last: dict[str, str] = {}
        if path.exists():
            self.last = json.loads(path.read_text())

    def check(self, cls: str, now: datetime) -> None:
        rule = RULES.get(cls)
        if rule is None:
            raise PolicyError(f"No usage rule defined for class '{cls}'; refusing to query it.")
        prev = self.last.get(cls)
        prev_dt = datetime.fromisoformat(prev) if prev else None
        if rule.daily_after_1700:
            if now.hour < 17:
                raise PolicyError(
                    f"'{cls}' may only be queried after 17:00 UTC (it is {now:%H:%M} UTC)."
                )
            if prev_dt and prev_dt.date() == now.date():
                raise PolicyError(
                    f"'{cls}' was already queried today at {prev_dt:%H:%M} UTC; limit is once per day."
                )
        if rule.min_interval and prev_dt and now - prev_dt < rule.min_interval:
            raise PolicyError(
                f"'{cls}' was queried at {prev_dt:%Y-%m-%d %H:%M} UTC; "
                f"wait until {(prev_dt + rule.min_interval):%Y-%m-%d %H:%M} UTC."
            )

    def record(self, cls: str, now: datetime) -> None:
        self.last[cls] = now.isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.last, indent=2, sort_keys=True))


class RateLimiter:
    """Sliding-window limiter, conservative relative to the 30/min, 300/hr policy."""

    def __init__(self, per_minute: int = 20, per_hour: int = 200, min_gap: float = 2.5,
                 clock=time.monotonic, sleep=time.sleep):
        self.per_minute, self.per_hour, self.min_gap = per_minute, per_hour, min_gap
        self.clock, self.sleep = clock, sleep
        self.stamps: deque[float] = deque()

    def wait(self) -> None:
        while True:
            now = self.clock()
            while self.stamps and now - self.stamps[0] > 3600:
                self.stamps.popleft()
            delays = []
            if self.stamps:
                delays.append(self.stamps[-1] + self.min_gap - now)
            last_min = [t for t in self.stamps if now - t < 60]
            if len(last_min) >= self.per_minute:
                delays.append(last_min[0] + 60 - now)
            if len(self.stamps) >= self.per_hour:
                delays.append(self.stamps[0] + 3600 - now)
            delay = max(delays, default=0)
            if delay <= 0:
                self.stamps.append(now)
                return
            log.debug("rate limiter sleeping %.1fs", delay)
            self.sleep(delay)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SpaceTrack:
    def __init__(self, username: str, password: str, ledger: Ledger, *,
                 force: bool = False, timeout: float = 300, limiter: RateLimiter | None = None):
        if not username or not password:
            raise SpaceTrackError("Space-Track username and password are required.")
        self.username, self.password = username, password
        self.ledger = ledger
        self.force = force
        self.timeout = timeout
        self.limiter = limiter or RateLimiter()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.logged_in = False
        self.requests_made = 0

    # -- session ---------------------------------------------------------
    def __enter__(self) -> "SpaceTrack":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def login(self) -> None:
        self.limiter.wait()
        self.requests_made += 1
        resp = self.session.post(f"{BASE_URL}/ajaxauth/login",
                                 data={"identity": self.username, "password": self.password},
                                 timeout=self.timeout)
        if resp.status_code != 200 or "Failed" in resp.text or "chocolatechip" not in self.session.cookies:
            raise SpaceTrackError(f"Login failed (HTTP {resp.status_code}). Check SPACETRACK_USER/SPACETRACK_PASS.")
        self.logged_in = True

    def close(self) -> None:
        if self.logged_in:
            try:
                self.limiter.wait()
                self.session.get(f"{BASE_URL}/ajaxauth/logout", timeout=30)
            except requests.RequestException:
                pass
            self.logged_in = False
        self.session.close()

    # -- queries ---------------------------------------------------------
    def _get(self, path: str) -> list[dict]:
        url = f"{BASE_URL}/basicspacedata/query/{quote(path, safe='/,<>-=:')}"
        for attempt in range(4):
            self.limiter.wait()
            self.requests_made += 1
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                err = f"network error: {e}"
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("error"):
                        raise SpaceTrackError(f"API error: {data['error']}")
                    return data
                if resp.status_code == 401:
                    raise SpaceTrackError("Not authorized (session expired or bad credentials).")
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise SpaceTrackError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                err = f"HTTP {resp.status_code}"
            wait = 60 * (attempt + 1)  # back off generously; never hammer the server
            log.warning("Space-Track %s; retrying in %ss", err, wait)
            time.sleep(wait)
        raise SpaceTrackError(f"Giving up after repeated failures: {err}")

    def fetch(self, cls: str, filters: Sequence[str] = (), *, ids: Iterable[int] | None = None,
              orderby: str | None = None, predicates: Sequence[str] | None = None) -> list[dict]:
        """Run one logical query against `cls`, checked against the usage ledger.

        `filters` are REST path segments such as "DECAY/null-val". If `ids` is given the
        query is split into comma-delimited chunks of NORAD ids (still one logical query).
        """
        now = utcnow()
        if self.force:
            log.warning("--force: skipping usage-policy check for '%s'", cls)
        else:
            self.ledger.check(cls, now)
        if not self.logged_in:
            self.login()
        self.ledger.record(cls, now)

        id_list = sorted(set(int(i) for i in ids)) if ids is not None else None
        if id_list is not None and not id_list:
            return []
        chunks = ([id_list[i:i + ID_CHUNK] for i in range(0, len(id_list), ID_CHUNK)]
                  if id_list is not None else [None])
        out: list[dict] = []
        for chunk in chunks:
            parts = [f"class/{cls}", *filters]
            if chunk is not None:
                parts.append("NORAD_CAT_ID/" + ",".join(map(str, chunk)))
            if orderby:
                parts.append(f"orderby/{orderby}")
            if predicates:
                parts.append("predicates/" + ",".join(predicates))
            parts.append("format/json")
            out.extend(self._get("/".join(parts)))
        log.info("%s: %d rows", cls, len(out))
        return out
