from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, Sequence


class DataFrame:  # type: ignore
    def __init__(self, data):
        self._rows: List[Dict[str, object]]
        self._columns: List[str]
        if isinstance(data, dict):
            keys = list(data.keys())
            lengths = [len(data[key]) for key in keys]
            if lengths and len(set(lengths)) != 1:
                raise ValueError("All columns must be the same length")
            self._columns = keys
            row_count = lengths[0] if lengths else 0
            self._rows = []
            for idx in range(row_count):
                row = {key: data[key][idx] for key in keys}
                self._rows.append(row)
        else:
            self._rows = [dict(row) for row in data]
            column_names = set()
            for row in self._rows:
                column_names.update(row.keys())
            self._columns = list(column_names)

    def to_csv(self, path, index: bool = False) -> None:
        del index
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            columns = self._columns
            if not columns and self._rows:
                columns = list(self._rows[0].keys())
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in self._rows:
                writer.writerow({col: row.get(col) for col in columns})

    def __getitem__(self, item: str) -> List[object]:
        return [row.get(item) for row in self._rows]

    def __iter__(self):
        return iter(self._rows)

    @property
    def columns(self) -> Sequence[str]:
        return self._columns


def read_csv(path):
    path = Path(path)
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
    return DataFrame(rows)
