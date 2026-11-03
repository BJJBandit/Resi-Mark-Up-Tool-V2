"""Per-job design trace.

Every engine records what it did as a tree of :class:`Step` objects: the inputs it read,
the rule it applied (in plain English), the clause or table it cites, the result and a
status. The trace is what the engineer audits, so nothing an engine decides is allowed to
happen outside a step.

Statuses
--------
``ok``             the step completed from verified inputs and loaded tables
``flag``           completed, but the engineer must look (disagreement, assumption, upgrade)
``blocked``        could not complete – a table is not loaded or a required input is missing;
                   the step's ``citation`` says exactly what is needed
``not_supported``  the practice has decided not to support this branch (tile roofs, waffle
                   rafts, cyclonic regions …); the engine stops rather than guess

Load paths (roof → wall → beam → column → footing) are also kept here as ``load_path``
steps because the practice decided they are recorded in the trace, not issued as a sheet.

The trace serialises to JSON (``review.json``), to Markdown for the review page and to a
Mermaid flowchart of the steps actually taken. The *whole* decision tree with the taken
path highlighted is rendered by :mod:`app.engines.logic`, which knows the tree.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["ok", "flag", "blocked", "not_supported"]
StepKind = Literal["step", "decision", "lookup", "load_path", "scope"]

_SEVERITY: dict[str, int] = {"ok": 0, "flag": 1, "not_supported": 2, "blocked": 3}


def worst(*statuses: str) -> Status:
    """The status that should represent a group of steps (blocked beats everything)."""
    if not statuses:
        return "ok"
    return max(statuses, key=lambda s: _SEVERITY.get(s, 0))  # type: ignore[return-value]


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: str
    title: str
    kind: StepKind = "step"
    inputs: dict[str, Any] = Field(default_factory=dict)
    rule: str = Field("", description="Plain-English sentence of the logic applied.")
    citation: str = Field("", description="Standard / clause / table, e.g. 'AS 2870:2011 Table 3.4'.")
    result: dict[str, Any] = Field(default_factory=dict)
    status: Status = "ok"
    preference_applied: str | None = Field(None, description="preferences.yaml key that supplied the value.")
    conservatism_note: str | None = Field(
        None, description="Set when a standard overrode a preference or a lookup was rounded up."
    )
    node_id: str | None = Field(None, description="Logic-tree node this step corresponds to, if any.")
    children: list["Step"] = Field(default_factory=list)

    # load_path only
    load_from: str | None = None
    load_to: str | None = None

    @property
    def rolled_status(self) -> Status:
        """This step's status, or the worst of its children if that is worse."""
        return worst(self.status, *(c.rolled_status for c in self.children))

    def walk(self) -> Iterator["Step"]:
        yield self
        for c in self.children:
            yield from c.walk()


class Trace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_name: str = ""
    job_no: str = ""
    building_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    steps: list[Step] = Field(default_factory=list)

    # the open-step stack is runtime state, not part of the serialised trace
    _stack: list[Step] = []

    def model_post_init(self, __context: Any) -> None:  # noqa: D401
        self._stack = []

    # -- recording -----------------------------------------------------------------

    def add(self, step: Step) -> Step:
        """Append a step under the currently open step (or at the top level)."""
        if self._stack:
            self._stack[-1].children.append(step)
        else:
            self.steps.append(step)
        return step

    def step(self, engine: str, title: str, **kw: Any) -> Step:
        return self.add(Step(engine=engine, title=title, **kw))

    @contextmanager
    def section(self, engine: str, title: str, **kw: Any) -> Iterator[Step]:
        """Open a parent step; everything recorded inside becomes its child.

        On exit the parent's status is raised to the worst child status unless the engine
        set it explicitly to something worse.
        """
        parent = self.add(Step(engine=engine, title=title, **kw))
        self._stack.append(parent)
        try:
            yield parent
        finally:
            self._stack.pop()
            parent.status = worst(parent.status, *(c.rolled_status for c in parent.children))

    def load_path(self, engine: str, load_from: str, load_to: str, rule: str = "", **kw: Any) -> Step:
        """Record one link of the load path, e.g. roof → external walls."""
        return self.add(
            Step(
                engine=engine,
                kind="load_path",
                title=f"{load_from} → {load_to}",
                load_from=load_from,
                load_to=load_to,
                rule=rule,
                **kw,
            )
        )

    # -- queries ---------------------------------------------------------------------

    def all_steps(self) -> Iterator[Step]:
        for s in self.steps:
            yield from s.walk()

    @property
    def status(self) -> Status:
        return worst(*(s.rolled_status for s in self.steps))

    def blocked_on(self) -> list[str]:
        """Citations of everything that stopped an engine – de-duplicated, in order."""
        out: list[str] = []
        for s in self.all_steps():
            # a parent's status is only a roll-up of its children – the children carry the citation
            if s.children:
                continue
            if s.status == "blocked" and s.citation and s.citation not in out:
                out.append(s.citation)
        return out

    def flags(self) -> list[str]:
        return [f"{s.title}: {s.rule}" if s.rule else s.title for s in self.all_steps() if s.status == "flag"]

    def load_paths(self) -> list[Step]:
        return [s for s in self.all_steps() if s.kind == "load_path"]

    # -- rendering -------------------------------------------------------------------

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=indent)

    def to_markdown(self) -> str:
        lines = [f"# Design trace – {self.project_name or 'project'} {self.job_no}".rstrip()]
        if self.building_id:
            lines.append(f"Building: `{self.building_id}`  ")
        lines.append(f"Overall status: **{self.status}**")
        if self.blocked_on():
            lines.append("")
            lines.append("Blocked on:")
            lines += [f"- {c}" for c in self.blocked_on()]
        lines.append("")
        for s in self.steps:
            _md_step(s, 0, lines)
        paths = self.load_paths()
        if paths:
            lines += ["", "## Load paths", ""]
            lines += [f"- {p.load_from} → {p.load_to}" + (f" — {p.rule}" if p.rule else "") for p in paths]
        return "\n".join(lines) + "\n"

    def to_mermaid(self) -> str:
        """Flowchart of the steps taken, in order, coloured by status.

        Nested steps become subgraphs; load_path steps form their own chain.
        """
        out = ["flowchart TD"]
        out += _MERMAID_CLASSES
        counter = [0]
        prev: str | None = None
        for s in self.steps:
            if s.kind == "load_path":
                continue
            node = _mm_step(s, out, counter)
            if prev:
                out.append(f"  {prev} --> {node}")
            prev = node
        paths = self.load_paths()
        if paths:
            out.append("  subgraph LP[Load path]")
            ids: dict[str, str] = {}
            for p in paths:
                for label in (p.load_from, p.load_to):
                    if label not in ids:
                        ids[label] = f"lp{len(ids)}"
                        out.append(f"    {ids[label]}([{_esc(label)}])")
                out.append(f"    {ids[p.load_from]} --> {ids[p.load_to]}")
            out.append("  end")
        return "\n".join(out) + "\n"


_MERMAID_CLASSES = [
    "  classDef ok fill:#e8f5e9,stroke:#2e7d32,color:#1b1b1b",
    "  classDef flag fill:#fff8e1,stroke:#f9a825,color:#1b1b1b",
    "  classDef blocked fill:#ffebee,stroke:#c62828,color:#1b1b1b",
    "  classDef not_supported fill:#eceff1,stroke:#607d8b,color:#1b1b1b",
]


def _esc(text: str) -> str:
    """Make text safe inside a Mermaid node label."""
    return str(text).replace('"', "'").replace("[", "(").replace("]", ")").replace("{", "(").replace("}", ")").replace("|", "/").replace("\n", " ")


def _mm_step(s: Step, out: list[str], counter: list[int]) -> str:
    counter[0] += 1
    nid = f"s{counter[0]}"
    label = _esc(s.title)
    if s.citation:
        label += f"<br/><i>{_esc(s.citation)}</i>"
    if s.children:
        out.append(f"  subgraph {nid}_g[\"{label}\"]")
        prev = None
        first = None
        for c in s.children:
            if c.kind == "load_path":
                continue
            cid = _mm_step(c, out, counter)
            first = first or cid
            if prev:
                out.append(f"    {prev} --> {cid}")
            prev = cid
        out.append("  end")
        out.append(f"  class {nid}_g {s.rolled_status}")
        return f"{nid}_g"
    shape = "{{{{{}}}}}" if s.kind == "decision" else ("[[{}]]" if s.kind == "lookup" else "[{}]")
    out.append(f"  {nid}{shape.format(chr(34) + label + chr(34))}")
    out.append(f"  class {nid} {s.status}")
    return nid


def _md_step(s: Step, depth: int, lines: list[str]) -> None:
    badge = {"ok": "OK", "flag": "FLAG", "blocked": "BLOCKED", "not_supported": "NOT SUPPORTED"}[s.rolled_status]
    head = "#" * min(depth + 2, 6)
    lines.append(f"{head} {s.title} — {badge}")
    if s.citation:
        lines.append(f"*{s.citation}*  ")
    if s.rule:
        lines.append(f"{s.rule}  ")
    if s.inputs:
        lines.append("Inputs: " + ", ".join(f"`{k}={v}`" for k, v in s.inputs.items()) + "  ")
    if s.result:
        lines.append("Result: " + ", ".join(f"`{k}={v}`" for k, v in s.result.items()) + "  ")
    if s.preference_applied:
        lines.append(f"Preference applied: `{s.preference_applied}`  ")
    if s.conservatism_note:
        lines.append(f"Conservatism: {s.conservatism_note}  ")
    lines.append("")
    for c in s.children:
        if c.kind != "load_path":
            _md_step(c, depth + 1, lines)
