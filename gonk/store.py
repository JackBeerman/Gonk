"""Local state: the on-orbit catalog snapshot, run history, and cached reference tables.

Layout of the state directory:
    catalog.json.gz   on-orbit snapshot {"meta": {...}, "objects": {norad: record}}
    history.jsonl     one JSON line per run (counts + ids), used for trend charts
    refs.json         country and launch-site names (cached; refreshed weekly)
    ledger.json       when each Space-Track class was last queried (see spacetrack.py)
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime
from pathlib import Path


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.catalog_path = self.root / "catalog.json.gz"
        self.history_path = self.root / "history.jsonl"
        self.refs_path = self.root / "refs.json"
        self.ledger_path = self.root / "ledger.json"

    # -- snapshot --------------------------------------------------------
    def has_baseline(self) -> bool:
        return self.catalog_path.exists()

    def load_catalog(self) -> tuple[dict, dict[int, dict]]:
        with gzip.open(self.catalog_path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        return data["meta"], {int(k): v for k, v in data["objects"].items()}

    def save_catalog(self, meta: dict, objects: dict[int, dict]) -> None:
        tmp = self.catalog_path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"meta": meta, "objects": {str(k): objects[k] for k in sorted(objects)}},
                      f, separators=(",", ":"))
        os.replace(tmp, self.catalog_path)  # atomic: never leave a half-written snapshot

    # -- history ---------------------------------------------------------
    def append_history(self, entry: dict) -> None:
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def load_history(self) -> list[dict]:
        if not self.history_path.exists():
            return []
        return [json.loads(line) for line in self.history_path.read_text().splitlines() if line.strip()]

    # -- reference tables ------------------------------------------------
    def load_refs(self) -> dict:
        if self.refs_path.exists():
            return json.loads(self.refs_path.read_text())
        return {}

    def save_refs(self, refs: dict) -> None:
        self.refs_path.write_text(json.dumps(refs, indent=1, sort_keys=True))


def age_days(iso: str | None, now: datetime) -> float:
    if not iso:
        return float("inf")
    return (now - datetime.fromisoformat(iso)).total_seconds() / 86400
