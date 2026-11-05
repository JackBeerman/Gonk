"""CSV dataset kept across runs, in the data folder (default ./data):

    baseline_YYYY-MM-DD.csv   every object in orbit when the baseline was taken
    new_YYYY-MM-DD.csv        objects added to the catalog in that day's run
    removed_YYYY-MM-DD.csv    objects that left orbit in that day's run
    orbit_updates_YYYY-MM-DD.csv  earlier new objects whose first orbit arrived that day
    new_all.csv               every new object from every run, with a run_date column
    removed_all.csv           every removed object from every run, with a run_date column
    orbit_updates_all.csv     every orbit update from every run, with a run_date column

A daily file is written even on a day with nothing new (header only), so the series
has no gaps. All files share the same columns (report.CSV_COLUMNS), so they can be
stacked in a spreadsheet or loaded together in pandas.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from . import confidence as conf
from .pipeline import RunResult
from .report import CSV_COLUMNS, to_csv


def _rate_baseline(objects: dict[int, dict], res: RunResult) -> list[dict]:
    rows = []
    for norad in sorted(objects):
        rec = dict(objects[norad])
        conf.assess_new(rec, res.run_at)
        rows.append(rec)
    return rows


def _append(path: Path, run_date: str, records: list[dict]) -> None:
    """Add this run's rows to a cumulative file, replacing any earlier rows for the same date."""
    kept: list[dict] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            kept = [row for row in csv.DictReader(f) if row.get("run_date") != run_date]
    new_rows = list(csv.DictReader(io.StringIO(to_csv(records, {"run_date": run_date}))))
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["run_date"] + CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept + new_rows)
    tmp.replace(path)


def export(res: RunResult, data_dir: Path) -> list[Path]:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    date = res.run_at.date().isoformat()
    written: list[Path] = []
    if res.kind == "baseline":
        path = data_dir / f"baseline_{date}.csv"
        path.write_text(to_csv(_rate_baseline(res.objects, res)), encoding="utf-8")
        written.append(path)
        return written
    for name, records in (("new", res.new), ("removed", res.gone), ("orbit_updates", res.orbit_updates)):
        path = data_dir / f"{name}_{date}.csv"
        path.write_text(to_csv(records), encoding="utf-8")
        _append(data_dir / f"{name}_all.csv", date, records)
        written += [path, data_dir / f"{name}_all.csv"]
    return written
