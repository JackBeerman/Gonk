"""Offline tests: no network, no Space-Track account needed.  Run: python -m unittest -v"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gonk import catalog as cat
from gonk import emailer, pipeline, report
from gonk.spacetrack import Ledger, PolicyError, RateLimiter
from gonk.store import Store


def satcat_row(norad, name, otype="PAYLOAD", country="US", launch="2026-10-01", intldes=None,
               period="95.0", incl="53.0", apo="550", peri="540", decay=None, **extra):
    return {"NORAD_CAT_ID": str(norad), "OBJECT_NAME": name, "OBJECT_TYPE": otype, "COUNTRY": country,
            "LAUNCH": launch, "SITE": "AFETR", "DECAY": decay, "PERIOD": period, "INCLINATION": incl,
            "APOGEE": apo, "PERIGEE": peri, "RCS_SIZE": "LARGE",
            "OBJECT_ID": intldes or f"{launch[:4]}-001A", **extra}


class FakeSpaceTrack:
    """Stands in for gonk.spacetrack.SpaceTrack; returns canned rows per class."""

    def __init__(self, data: dict[str, list[dict]]):
        self.data = data
        self.calls: list[tuple] = []

    def fetch(self, cls, filters=(), *, ids=None, orderby=None, predicates=None):
        self.calls.append((cls, tuple(filters), tuple(sorted(ids)) if ids is not None else None))
        rows = self.data.get(cls, [])
        if ids is not None:
            rows = [r for r in rows if int(r["NORAD_CAT_ID"]) in set(ids)]
        return rows

    def close(self):
        pass


REFS = {
    "boxscore": [{"SPADOC_CD": "US", "COUNTRY": "UNITED STATES"},
                 {"SPADOC_CD": "PRC", "COUNTRY": "PEOPLES REPUBLIC OF CHINA"}],
    "launch_site": [{"SITE_CODE": "AFETR", "LAUNCH_SITE": "AIR FORCE EASTERN TEST RANGE"}],
}

UTC = timezone.utc


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_daily_class_only_after_1700_utc(self):
        with self.assertRaises(PolicyError):
            self.ledger.check("satcat", datetime(2026, 11, 5, 16, 59, tzinfo=UTC))
        self.ledger.check("satcat", datetime(2026, 11, 5, 17, 1, tzinfo=UTC))

    def test_daily_class_once_per_utc_day_and_persists(self):
        t = datetime(2026, 11, 5, 17, 30, tzinfo=UTC)
        self.ledger.record("satcat_debut", t)
        reloaded = Ledger(self.ledger.path)
        with self.assertRaises(PolicyError):
            reloaded.check("satcat_debut", t + timedelta(hours=5))
        reloaded.check("satcat_debut", t + timedelta(days=1))

    def test_gp_once_per_hour(self):
        t = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
        self.ledger.record("gp", t)
        with self.assertRaises(PolicyError):
            self.ledger.check("gp", t + timedelta(minutes=59))
        self.ledger.check("gp", t + timedelta(minutes=61))

    def test_unknown_class_refused(self):
        with self.assertRaises(PolicyError):
            self.ledger.check("gp_history", datetime(2026, 11, 5, 18, tzinfo=UTC))


class RateLimiterTests(unittest.TestCase):
    def test_stays_under_published_limits(self):
        clock = [0.0]
        lim = RateLimiter(clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        for _ in range(250):
            lim.wait()
        stamps = list(lim.stamps)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertGreaterEqual(min(gaps), 2.5 - 1e-9)
        for i, t in enumerate(stamps):
            in_minute = sum(1 for s in stamps[i:] if s - t < 60)
            self.assertLess(in_minute, 30)
            in_hour = sum(1 for s in stamps[i:] if s - t < 3600)
            self.assertLess(in_hour, 300)


class OrbitTests(unittest.TestCase):
    def rec(self, peri, apo, incl, period):
        return cat.from_satcat(satcat_row(1, "X", peri=str(peri), apo=str(apo), incl=str(incl), period=str(period)))

    def test_regimes(self):
        self.assertEqual(self.rec(280, 290, 70.0, 90.2)["regime"], "LEO")
        self.assertEqual(self.rec(20100, 20260, 55.0, 718)["regime"], "MEO")
        self.assertEqual(self.rec(35780, 35795, 0.05, 1436.1)["regime"], "GEO")
        self.assertEqual(self.rec(35700, 35850, 8.0, 1436.0)["regime"], "GSO (inclined)")
        self.assertEqual(self.rec(200, 35800, 27.0, 630)["regime"], "GTO")
        self.assertEqual(self.rec(500, 39000, 63.4, 717)["regime"], "HEO")

    def test_sun_synchronous(self):
        self.assertTrue(self.rec(510, 530, 97.46, 95.0)["sso"])
        self.assertFalse(self.rec(510, 530, 53.0, 95.0)["sso"])

    def test_constellation(self):
        self.assertEqual(cat.constellation("STARLINK-38431"), "Starlink (SpaceX)")
        self.assertEqual(cat.constellation("HULIANWANG DIGUI 223"), "Guowang / SatNet (China)")
        self.assertIsNone(cat.constellation("VANGUARD 1"))

    def test_gp_merge(self):
        rec = cat.from_satcat(satcat_row(9, "NEW", period=None, incl=None, apo=None, peri=None))
        self.assertEqual(rec["regime"], "Unknown")
        cat.apply_gp(rec, {"PERIOD": "90.164", "INCLINATION": "70.0016", "APOAPSIS": "285.1",
                           "PERIAPSIS": "279.9", "ECCENTRICITY": "0.00039647", "SEMIMAJOR_AXIS": "6660.6",
                           "EPOCH": "2026-11-04T08:00:03", "TLE_LINE1": "1 ...", "TLE_LINE2": "2 ..."})
        self.assertEqual(rec["regime"], "LEO")
        self.assertEqual(rec["tle1"], "1 ...")
        self.assertAlmostEqual(rec["revs_per_day"], 1440 / 90.16, places=2)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state")
        self.t0 = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
        base = [
            satcat_row(100, "OLDSAT 1"),
            satcat_row(101, "STARLINK-1001", launch="2026-01-10", intldes="2026-010A"),
            satcat_row(102, "OBJECT A", country="PRC", launch="2026-10-23", intldes="2026-241A"),
            satcat_row(103, "SOME DEB", otype="DEBRIS", launch="1999-01-01", intldes="1999-001C"),
            satcat_row(104, "DECAYED ALREADY", decay="2025-01-01"),  # must be skipped
        ]
        st = FakeSpaceTrack({"satcat": base, **REFS})
        self.base = pipeline.run_baseline(st, self.store, self.t0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_baseline(self):
        self.assertEqual(set(self.base.objects), {100, 101, 102, 103})
        self.assertEqual(self.base.objects[102]["country"], "Peoples Republic of China")
        self.assertEqual(self.base.objects[100]["site"], "Air Force Eastern Test Range")
        rep = report.build(self.base)
        self.assertIn("4 objects in orbit", rep.subject)
        self.assertIn("catalog.zip", rep.attachments)

    def daily_data(self):
        return {
            "satcat_debut": [
                satcat_row(200, "NEWSAT-1", launch="2026-11-03", intldes="2026-250A", period=None,
                           incl=None, apo=None, peri=None, DEBUT="2026-11-05 17:16:41"),
                satcat_row(201, "FALCON 9 R/B", otype="ROCKET BODY", launch="2026-11-03",
                           intldes="2026-250B", DEBUT="2026-11-05 17:16:41"),
                satcat_row(101, "STARLINK-1001", DEBUT="2026-11-04 17:00:00"),  # already known: overlap
                satcat_row(202, "BLINK", launch="2026-11-02", intldes="2026-249A", decay="2026-11-05",
                           DEBUT="2026-11-05 17:16:41"),
            ],
            "satcat_change": [
                {"NORAD_CAT_ID": "101", "CURRENT_NAME": "STARLINK-1001", "PREVIOUS_NAME": None,
                 "CURRENT_DECAY": "2026-11-05", "PREVIOUS_DECAY": None, "CHANGE_MADE": "2026-11-05 17:16:41"},
                {"NORAD_CAT_ID": "102", "CURRENT_NAME": "SHIYAN 30", "PREVIOUS_NAME": "OBJECT A",
                 "CURRENT_COUNTRY": "PRC", "PREVIOUS_COUNTRY": None, "CURRENT_DECAY": None,
                 "CHANGE_MADE": "2026-11-05 17:16:41"},
            ],
            "decay": [
                {"NORAD_CAT_ID": "101", "MSG_TYPE": "Historical", "DECAY_EPOCH": "2026-11-05 2:55:00",
                 "SOURCE": "TIP_msg", "MSG_EPOCH": "2026-11-05 03:10:00"},
                {"NORAD_CAT_ID": "100", "MSG_TYPE": "Prediction", "DECAY_EPOCH": "2026-11-08 12:00:00",
                 "SOURCE": "TIP_msg", "MSG_EPOCH": "2026-11-05 10:00:00"},
                {"NORAD_CAT_ID": "103", "MSG_TYPE": "Prediction", "DECAY_EPOCH": "2027-03-01 0:00:00",
                 "SOURCE": "60day_msg", "MSG_EPOCH": "2026-11-05 10:00:00"},
            ],
            "gp": [{"NORAD_CAT_ID": "200", "PERIOD": "95.5", "INCLINATION": "97.6", "APOAPSIS": "560",
                    "PERIAPSIS": "550", "ECCENTRICITY": "0.0007", "SEMIMAJOR_AXIS": "6933",
                    "EPOCH": "2026-11-05T06:00:00", "TLE_LINE1": "1 L1", "TLE_LINE2": "2 L2"}],
        }

    def test_daily_incremental(self):
        st = FakeSpaceTrack(self.daily_data())
        t1 = self.t0 + timedelta(days=1)
        res = pipeline.run_daily(st, self.store, t1)
        self.assertFalse(res.full_sync)
        # Query window overlaps the previous run by a day.
        debut_call = next(c for c in st.calls if c[0] == "satcat_debut")
        self.assertEqual(debut_call[1], ("DEBUT/>2026-11-03",))
        # GP is fetched only for new objects still in orbit, in one call.
        self.assertEqual([c for c in st.calls if c[0] == "gp"], [("gp", (), (200, 201))])

        self.assertEqual(sorted(r["norad"] for r in res.new), [200, 201, 202])
        new200 = next(r for r in res.new if r["norad"] == 200)
        self.assertEqual(new200["regime"], "LEO")
        self.assertTrue(new200["sso"])
        self.assertEqual(new200["tle2"], "2 L2")
        self.assertEqual(new200["country"], "United States")

        self.assertEqual([g["norad"] for g in res.gone], [101])
        self.assertEqual(res.gone[0]["decay_epoch"], "2026-11-05 2:55:00")
        self.assertEqual(res.gone[0]["removal_reason"], "Re-entered (decayed)")
        self.assertEqual(res.gone[0]["days_in_orbit"], 299)

        self.assertEqual(res.renamed[0]["changes"]["name"], ("OBJECT A", "SHIYAN 30"))
        self.assertEqual([p["norad"] for p in res.predictions], [100])  # 103 is outside 7-day window

        meta, objects = self.store.load_catalog()
        self.assertEqual(set(objects), {100, 102, 103, 200, 201})  # 202 decayed before we saw it
        self.assertEqual(objects[102]["name"], "SHIYAN 30")

        rep = report.build(res)
        self.assertIn("3 new, 1 removed", rep.subject)
        for needle in ("NEWSAT-1", "already re-entered", "SHIYAN 30", "Predicted re-entries", "LEO · SSO"):
            self.assertIn(needle, rep.html)
        self.assertEqual(set(rep.attachments), {"new_objects.csv", "removed_objects.csv", "predicted_reentries.csv"})
        self.assertIn("tle1", rep.attachments["new_objects.csv"].splitlines()[0])

    def test_daily_is_idempotent_over_overlap(self):
        data = self.daily_data()
        pipeline.run_daily(FakeSpaceTrack(data), self.store, self.t0 + timedelta(days=1))
        res2 = pipeline.run_daily(FakeSpaceTrack(data), self.store, self.t0 + timedelta(days=2))
        self.assertEqual(res2.new, [])
        self.assertEqual(res2.gone, [])
        self.assertEqual(res2.renamed, [])
        hist = self.store.load_history()
        self.assertEqual([h["kind"] for h in hist], ["baseline", "daily", "daily"])
        self.assertIn("Last 30 days", report.build(res2).html)

    def test_weekly_full_sync_catches_silent_removals(self):
        current = [satcat_row(100, "OLDSAT 1"), satcat_row(101, "STARLINK-1001", launch="2026-01-10"),
                   satcat_row(103, "SOME DEB", otype="DEBRIS"), satcat_row(300, "LATE ENTRY", launch="2026-10-01")]
        st = FakeSpaceTrack({"satcat": current, "gp": []})
        res = pipeline.run_daily(st, self.store, self.t0 + timedelta(days=8))
        self.assertTrue(res.full_sync)
        self.assertEqual([r["norad"] for r in res.new], [300])
        self.assertEqual([g["norad"] for g in res.gone], [102])
        self.assertTrue(res.gone[0]["removal_reason"].startswith("Removed from public catalog"))


class EmailTests(unittest.TestCase):
    def test_message_has_html_text_and_csv(self):
        rep = report.Report(subject="S", html="<b>hi</b>", text="hi", attachments={"a.csv": "x,y\n1,2\n"})
        msg = emailer.build_message(rep, "me@example.com", ["you@example.com", "them@example.com"])
        self.assertEqual(msg["To"], "you@example.com, them@example.com")
        types = [p.get_content_type() for p in msg.walk()]
        self.assertIn("text/plain", types)
        self.assertIn("text/html", types)
        self.assertIn("text/csv", types)


if __name__ == "__main__":
    unittest.main()
