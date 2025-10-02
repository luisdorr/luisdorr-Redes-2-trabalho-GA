from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, List, Sequence


class _Axes:
    def __init__(self) -> None:
        self._bars: List[tuple] = []

    def bar(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - bookkeeping only
        self._bars.append((args, kwargs))

    def set_ylabel(self, label: str) -> None:  # pragma: no cover - stub
        self._ylabel = label

    def set_title(self, title: str) -> None:  # pragma: no cover - stub
        self._title = title

    def set_xticks(self, ticks: Sequence[float]) -> None:  # pragma: no cover - stub
        self._xticks = list(ticks)

    def set_xticklabels(self, labels: Iterable[str]) -> None:  # pragma: no cover - stub
        self._xticklabels = list(labels)

    def legend(self) -> None:  # pragma: no cover - stub
        self._legend = True


class _Figure:
    def __init__(self) -> None:
        self._saved_paths: List[Path] = []

    def tight_layout(self) -> None:  # pragma: no cover - stub
        return None

    def savefig(self, path: Any) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stub")
        self._saved_paths.append(path)


def subplots(figsize: Any = None):  # pragma: no cover - stub
    return _Figure(), _Axes()


def close(fig: Any) -> None:  # pragma: no cover - stub
    return None
