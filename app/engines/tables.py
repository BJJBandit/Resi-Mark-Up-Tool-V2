"""Standards table loader and conservative lookup.

Every deemed-to-satisfy table the engines consume lives as a YAML file (optionally with a
CSV sidecar for big span tables) under ``app/data/standards/<standard>/``. The file carries
the citation (standard, edition, table number, title) and the rows. Rows are transcribed
from the practice's licensed copies and are **never** filled in here: a table whose
``rows`` list is empty raises :class:`TableNotLoaded` so the engine reports
"AS 1684.2:2021 Table 8.18 – not loaded" instead of guessing.

Lookup convention
-----------------
* Categorical inputs (site class, construction type, terrain category …) must match a row
  exactly.
* Numeric inputs follow the standard's reading rule, declared per table in
  ``numeric_rule``:

  - ``ceil``  – pick the row whose tabulated value is the *next bound at or above* the
    demand (e.g. span tables: a 2.9 m span is read at the 3.0 m column). This is the
    conservative default.
  - ``floor`` – pick the row whose tabulated value is the *largest at or below* the
    demand (used where the tabulated quantity is a capacity that the design value must not
    exceed, e.g. a bearing pressure column read downwards).
  - ``exact`` – the value must appear in the table.

  Linear interpolation between bracketing rows is only done when the table declares
  ``interpolate: true``, because most deemed-to-satisfy tables forbid it.
* A blank output cell (a dash in the standard) means "not permitted / outside the table"
  and raises :class:`OutOfRange`.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

STANDARDS_DIR = Path(__file__).resolve().parents[1] / "data" / "standards"

NumericRule = str  # "ceil" | "floor" | "exact"
_VALID_RULES = {"ceil", "floor", "exact"}


# ---------------------------------------------------------------------------
# Errors – both carry the citation so a trace can quote it verbatim
# ---------------------------------------------------------------------------


class TableError(Exception):
    table_id: str
    citation: str


class TableNotLoaded(TableError):
    """The table file exists but has no rows (values not yet transcribed)."""

    def __init__(self, table_id: str, citation: str) -> None:
        self.table_id = table_id
        self.citation = citation
        super().__init__(f"{citation} – not loaded (table '{table_id}' has no rows)")


class TableMissing(TableError):
    """No file exists for the requested table id at all."""

    def __init__(self, table_id: str) -> None:
        self.table_id = table_id
        self.citation = table_id
        super().__init__(f"table '{table_id}' – no file under {STANDARDS_DIR}")


class OutOfRange(TableError):
    """The table is loaded but no row covers these inputs."""

    def __init__(self, table_id: str, citation: str, inputs: dict[str, Any], detail: str = "") -> None:
        self.table_id = table_id
        self.citation = citation
        self.inputs = inputs
        self.detail = detail
        extra = f" ({detail})" if detail else ""
        super().__init__(f"{citation} – inputs {inputs} outside tabulated range{extra}")


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _coerce(v: Any) -> Any:
    """CSV cells arrive as strings; turn numerics into numbers and blanks into None."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s == "" or s == "-" or s == "–":
            return None
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            return s
    return v


@dataclass
class Table:
    id: str  # "<folder>/<file stem>", e.g. "as4055_2021/table_2_2_region_A_wind_class"
    standard: str
    edition: str
    table: str
    title: str
    inputs: list[str]
    outputs: list[str]
    units: dict[str, str] = field(default_factory=dict)
    allowed: dict[str, list[Any]] = field(default_factory=dict)
    numeric_rule: dict[str, NumericRule] = field(default_factory=dict)
    interpolate: bool = False
    source_checked_by: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    path: Path | None = None
    notes: str = ""

    # -- metadata ----------------------------------------------------------------

    @property
    def citation(self) -> str:
        """e.g. 'AS 4055:2021 Table 2.2'."""
        label = self.table
        if label and not label.lower().startswith(("table", "cl", "fig", "clause")):
            label = f"Table {label}"
        head = f"{self.standard}:{self.edition}" if self.edition else self.standard
        return f"{head} {label}".strip()

    @property
    def loaded(self) -> bool:
        return bool(self.rows)

    def rule_for(self, column: str) -> NumericRule:
        return self.numeric_rule.get(column, "ceil")

    # -- lookup --------------------------------------------------------------------

    def lookup(self, **inputs: Any) -> dict[str, Any]:
        """Return the output columns for ``inputs``.

        Raises TableNotLoaded if there are no rows, OutOfRange if no row covers the inputs.
        """
        if not self.loaded:
            raise TableNotLoaded(self.id, self.citation)

        unknown = set(inputs) - set(self.inputs)
        if unknown:
            raise OutOfRange(self.id, self.citation, inputs, f"unknown input(s) {sorted(unknown)}")
        missing = [k for k in self.inputs if k not in inputs]
        if missing:
            raise OutOfRange(self.id, self.citation, inputs, f"missing input(s) {missing}")

        for k, v in inputs.items():
            if k in self.allowed and v not in self.allowed[k]:
                raise OutOfRange(self.id, self.citation, inputs, f"{k}={v!r} not in allowed {self.allowed[k]}")

        candidates = list(self.rows)

        # 1. categorical inputs – exact match
        for k, v in inputs.items():
            if not _is_number(v):
                candidates = [r for r in candidates if r.get(k) == v]
        if not candidates:
            raise OutOfRange(self.id, self.citation, inputs, "no row matches the categorical inputs")

        # 2. numeric inputs – conservative selection, one column at a time
        numeric_cols = [k for k, v in inputs.items() if _is_number(v)]
        interp_col: str | None = None
        for k in numeric_cols:
            demand = inputs[k]
            rule = self.rule_for(k)
            if rule not in _VALID_RULES:
                raise ValueError(f"{self.id}: numeric_rule for {k} must be one of {_VALID_RULES}, got {rule!r}")
            values = sorted({r[k] for r in candidates if _is_number(r.get(k))})
            if not values:
                raise OutOfRange(self.id, self.citation, inputs, f"no numeric rows for {k}")
            if rule == "exact":
                if demand not in values:
                    raise OutOfRange(self.id, self.citation, inputs, f"{k}={demand} not tabulated (exact rule)")
                chosen = demand
            elif rule == "ceil":
                above = [x for x in values if x >= demand]
                if not above:
                    raise OutOfRange(self.id, self.citation, inputs, f"{k}={demand} exceeds largest tabulated {values[-1]}")
                chosen = above[0]
            else:  # floor
                below = [x for x in values if x <= demand]
                if not below:
                    raise OutOfRange(self.id, self.citation, inputs, f"{k}={demand} below smallest tabulated {values[0]}")
                chosen = below[-1]

            if self.interpolate and demand not in values and interp_col is None and len(numeric_cols) == 1:
                interp_col = k
                lo = [x for x in values if x <= demand]
                hi = [x for x in values if x >= demand]
                if lo and hi:
                    candidates = [r for r in candidates if r[k] in (lo[-1], hi[0])]
                    continue
                interp_col = None
            candidates = [r for r in candidates if r.get(k) == chosen]

        if not candidates:
            raise OutOfRange(self.id, self.citation, inputs)

        if interp_col is not None and len(candidates) == 2:
            return self._interpolate(candidates, interp_col, inputs[interp_col], inputs)

        row = candidates[0]
        out = {k: row.get(k) for k in self.outputs}
        if any(v is None for v in out.values()):
            blank = [k for k, v in out.items() if v is None]
            raise OutOfRange(self.id, self.citation, inputs, f"table shows no value (dash) for {blank}")
        return out

    def _interpolate(self, rows: list[dict[str, Any]], col: str, x: float, inputs: dict[str, Any]) -> dict[str, Any]:
        a, b = sorted(rows, key=lambda r: r[col])
        x0, x1 = a[col], b[col]
        t = 0.0 if x1 == x0 else (x - x0) / (x1 - x0)
        out: dict[str, Any] = {}
        for k in self.outputs:
            va, vb = a.get(k), b.get(k)
            if va is None or vb is None:
                raise OutOfRange(self.id, self.citation, inputs, f"cannot interpolate across a dash in {k}")
            out[k] = va + (vb - va) * t if _is_number(va) and _is_number(vb) else va
        return out

    # -- construction ----------------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: Path, table_id: str | None = None) -> "Table":
        meta = yaml.safe_load(path.read_text()) or {}
        rows = meta.get("rows") or []
        csv_path = path.with_suffix(".csv")
        if csv_path.exists():
            with csv_path.open(newline="") as fh:
                rows = [{k: _coerce(v) for k, v in r.items()} for r in csv.DictReader(fh)]
        else:
            rows = [{k: _coerce(v) for k, v in r.items()} for r in rows]

        numeric_rule = meta.get("numeric_rule") or {}
        if isinstance(numeric_rule, str):  # one rule for every numeric column
            numeric_rule = {k: numeric_rule for k in meta.get("inputs", [])}

        return cls(
            id=table_id or f"{path.parent.name}/{path.stem}",
            standard=str(meta.get("standard", "")),
            edition=str(meta.get("edition", "")),
            table=str(meta.get("table", "")),
            title=str(meta.get("title", "")),
            inputs=list(meta.get("inputs") or []),
            outputs=list(meta.get("outputs") or []),
            units=dict(meta.get("units") or {}),
            allowed=dict(meta.get("allowed") or {}),
            numeric_rule=dict(numeric_rule),
            interpolate=bool(meta.get("interpolate", False)),
            source_checked_by=str(meta.get("source_checked_by") or ""),
            rows=rows,
            path=path,
            notes=str(meta.get("notes") or ""),
        )


# ---------------------------------------------------------------------------
# TableSet – everything under app/data/standards
# ---------------------------------------------------------------------------


def _is_lookup_table(path: Path) -> bool:
    """A lookup table declares ``inputs``/``outputs`` and ``rows`` (or has a CSV sidecar)."""
    try:
        meta = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return False
    return isinstance(meta, dict) and ("rows" in meta or "inputs" in meta or path.with_suffix(".csv").exists())


class TableSet:
    """All tables found under a standards directory, addressed by id."""

    def __init__(self, tables: Iterable[Table] = ()) -> None:
        self._tables: dict[str, Table] = {t.id: t for t in tables}

    @classmethod
    def load(cls, root: Path | str = STANDARDS_DIR) -> "TableSet":
        root = Path(root)
        tables = []
        for path in sorted(root.rglob("*.yaml")):
            if path.name.startswith("_"):
                continue
            if not _is_lookup_table(path):
                continue  # other data under standards/ (e.g. steel section catalogues) is not a lookup table
            tables.append(Table.from_yaml(path, table_id=path.relative_to(root).with_suffix("").as_posix()))
        return cls(tables)

    def add(self, table: Table) -> None:
        self._tables[table.id] = table

    def get(self, table_id: str) -> Table:
        try:
            return self._tables[table_id]
        except KeyError:
            raise TableMissing(table_id) from None

    def __contains__(self, table_id: str) -> bool:
        return table_id in self._tables

    def __iter__(self):
        return iter(sorted(self._tables.values(), key=lambda t: t.id))

    def __len__(self) -> int:
        return len(self._tables)

    def lookup(self, table_id: str, **inputs: Any) -> dict[str, Any]:
        return self.get(table_id).lookup(**inputs)

    def citation(self, table_id: str) -> str:
        """Citation for a table id, even if the file does not exist yet."""
        return self._tables[table_id].citation if table_id in self._tables else table_id

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "id": t.id,
                "citation": t.citation,
                "title": t.title,
                "loaded": t.loaded,
                "rows": len(t.rows),
                "source_checked_by": t.source_checked_by,
                "inputs": t.inputs,
                "outputs": t.outputs,
                "path": str(t.path.relative_to(STANDARDS_DIR)) if t.path and STANDARDS_DIR in t.path.parents else str(t.path or ""),
            }
            for t in self
        ]


_DEFAULT: TableSet | None = None


def default_tables(reload: bool = False) -> TableSet:
    """The repository's table set, loaded once."""
    global _DEFAULT
    if _DEFAULT is None or reload:
        _DEFAULT = TableSet.load()
    return _DEFAULT


def tables_status(tables: TableSet | None = None) -> list[dict[str, Any]]:
    """Every table: loaded?, row count, who checked it. Used by GET /tables/status."""
    return (tables or default_tables()).status()
