"""Decision logic as data: load, validate, walk and draw the engine trees in ``app/logic/``.

Each engine's decision tree is a YAML file the engineer can read and edit. The grammar is
deliberately tiny:

.. code-block:: yaml

    engine: footings_as2870
    title: Footing system selection (AS 2870:2011 Section 3)
    standard: AS 2870:2011
    inputs: [site_class, construction_type, floor_kind, rock_depth_mm]
    tables: [as2870_2011/table_3_4_strip_footings]      # dependencies, for the logic page
    start: scope
    nodes:
      - id: scope                      # DECISION node – `ask` names the input variable
        title: Site class in scope?
        ask: site_class
        citation: AS 2870:2011 cl 1.3
        branches:
          - when: {in: [P]}            # condition on the asked variable
            goto: class_p
          - when: {in: [A, S, M]}
            goto: floor_kind
          - else: true                 # fallback branch
            goto: unknown
      - id: class_p                    # RESULT node – sets outputs, may flag, continues or ends
        result: {footing_system: piers_to_rock}
        flag: Class P – engineered design required
        citation: AS 2870:2011 cl 1.3.3
        goto: pier_proposal
      - id: strip_lookup               # LOOKUP node – reads a standards table
        lookup: as2870_2011/table_3_4_strip_footings
        with: {site_class: site_class, construction_type: construction_type}
        into: strip_footing            # results[into] = table outputs; outputs also become variables
        on_not_loaded: blocked         # blocked (default) | not_supported
        goto: done
      - id: done
        end: true
      - id: unknown
        end: true
        status: blocked                # terminal status
        rule: Site class unknown – geotech report needed

Conditions (``when``) are maps with one or more of: ``in``, ``not_in``, ``eq``, ``ne``,
``lt``, ``le``, ``gt``, ``ge``, ``missing`` (true/false), ``same_as`` / ``differs_from``
(another variable's name). All keys in one map must hold. ``else: true`` always matches.
A ``when`` map may also name a different variable with ``var``.

Every node visited is recorded as a child :class:`~app.engines.trace.Step` of the engine's
section, carrying ``node_id`` so the renderer can highlight the path taken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .tables import OutOfRange, TableError, TableMissing, TableNotLoaded, TableSet
from .trace import Status, Step, Trace, worst

LOGIC_DIR = Path(__file__).resolve().parents[1] / "logic"

_MAX_VISITS = 200  # loop guard – trees are small


# ---------------------------------------------------------------------------
# Tree model
# ---------------------------------------------------------------------------


class LogicError(Exception):
    """A malformed tree (unknown goto target, duplicate id, …)."""


@dataclass
class Branch:
    when: dict[str, Any] | None  # None = else
    goto: str | None = None
    label: str = ""

    def describe(self, var: str) -> str:
        if self.label:
            return self.label
        if self.when is None:
            return "else"
        v = self.when.get("var", var)
        parts = []
        for op, val in self.when.items():
            if op == "var":
                continue
            if op == "in":
                parts.append(f"{v} in {_fmt_list(val)}")
            elif op == "not_in":
                parts.append(f"{v} not in {_fmt_list(val)}")
            elif op == "eq":
                parts.append(f"{v} = {val}")
            elif op == "ne":
                parts.append(f"{v} ≠ {val}")
            elif op == "lt":
                parts.append(f"{v} < {val}")
            elif op == "le":
                parts.append(f"{v} ≤ {val}")
            elif op == "gt":
                parts.append(f"{v} > {val}")
            elif op == "ge":
                parts.append(f"{v} ≥ {val}")
            elif op == "missing":
                parts.append(f"{v} {'missing' if val else 'given'}")
            elif op == "same_as":
                parts.append(f"{v} = {val}")
            elif op == "differs_from":
                parts.append(f"{v} ≠ {val}")
            else:
                parts.append(f"{op} {val}")
        return " and ".join(parts)


def _fmt_list(vals: Any) -> str:
    return "(" + ", ".join(str(v) for v in vals) + ")"


@dataclass
class Node:
    id: str
    title: str = ""
    kind: str = "end"  # decision | result | lookup | end
    ask: str | None = None
    branches: list[Branch] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    lookup: str | None = None
    with_: dict[str, Any] = field(default_factory=dict)
    into: str | None = None
    on_not_loaded: str = "blocked"
    goto: str | None = None
    end: bool = False
    status: Status | None = None
    flag: str | None = None
    rule: str = ""
    citation: str = ""
    preference: str | None = None

    @property
    def label(self) -> str:
        return self.title or self.id.replace("_", " ")


@dataclass
class LogicTree:
    engine: str
    title: str
    standard: str
    inputs: list[str]
    tables: list[str]
    start: str
    nodes: dict[str, Node]
    path: Path | None = None
    description: str = ""

    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise LogicError(f"{self.engine}: no node '{node_id}'") from None

    def validate(self) -> None:
        if self.start not in self.nodes:
            raise LogicError(f"{self.engine}: start node '{self.start}' not defined")
        for n in self.nodes.values():
            targets = [b.goto for b in n.branches] + [n.goto]
            for t in targets:
                if t is not None and t not in self.nodes:
                    raise LogicError(f"{self.engine}: node '{n.id}' points to unknown node '{t}'")
            if n.kind == "decision" and not n.ask:
                raise LogicError(f"{self.engine}: decision node '{n.id}' has no 'ask'")
            if n.kind == "decision" and not n.branches:
                raise LogicError(f"{self.engine}: decision node '{n.id}' has no branches")
            if n.kind in ("result", "lookup") and n.goto is None and not n.end:
                raise LogicError(f"{self.engine}: node '{n.id}' needs 'goto' or 'end: true'")

    def edges(self) -> list[tuple[str, str, str]]:
        """(from, to, label) for every edge in the tree."""
        out = []
        for n in self.nodes.values():
            for b in n.branches:
                if b.goto:
                    out.append((n.id, b.goto, b.describe(n.ask or "")))
            if n.goto and n.kind != "decision":
                out.append((n.id, n.goto, ""))
        return out


def _parse_node(raw: dict[str, Any]) -> Node:
    raw = dict(raw)
    node_id = raw.pop("id")
    branches = []
    for b in raw.pop("branches", []) or []:
        b = dict(b)
        is_else = bool(b.pop("else", False))
        when = None if is_else else b.pop("when", None)
        if when is None and not is_else:
            raise LogicError(f"branch of node '{node_id}' needs 'when' or 'else: true'")
        branches.append(Branch(when=when, goto=b.pop("goto", None), label=str(b.pop("label", ""))))
        if b:
            raise LogicError(f"branch of node '{node_id}' has unknown keys {sorted(b)}")
    if "ask" in raw:
        kind = "decision"
    elif "lookup" in raw:
        kind = "lookup"
    elif "result" in raw or ("goto" in raw and not raw.get("end")):
        kind = "result"
    else:
        kind = "end"
    node = Node(
        id=node_id,
        title=str(raw.pop("title", "")),
        kind=kind,
        ask=raw.pop("ask", None),
        branches=branches,
        result=dict(raw.pop("result", {}) or {}),
        lookup=raw.pop("lookup", None),
        with_=dict(raw.pop("with", {}) or {}),
        into=raw.pop("into", None),
        on_not_loaded=str(raw.pop("on_not_loaded", "blocked")),
        goto=raw.pop("goto", None),
        end=bool(raw.pop("end", False)),
        status=raw.pop("status", None),
        flag=raw.pop("flag", None),
        rule=str(raw.pop("rule", "")),
        citation=str(raw.pop("citation", "")),
        preference=raw.pop("preference", None),
    )
    if raw:
        raise LogicError(f"node '{node_id}' has unknown keys {sorted(raw)}")
    if node.flag and node.status is None:
        node.status = "flag"
    return node


def tree_from_dict(data: dict[str, Any], path: Path | None = None) -> LogicTree:
    nodes: dict[str, Node] = {}
    for raw in data.get("nodes") or []:
        n = _parse_node(raw)
        if n.id in nodes:
            raise LogicError(f"duplicate node id '{n.id}'")
        nodes[n.id] = n
    tree = LogicTree(
        engine=str(data["engine"]),
        title=str(data.get("title", data["engine"])),
        standard=str(data.get("standard", "")),
        inputs=list(data.get("inputs") or []),
        tables=list(data.get("tables") or []),
        start=str(data.get("start") or (next(iter(nodes)) if nodes else "")),
        nodes=nodes,
        path=path,
        description=str(data.get("description", "")),
    )
    tree.validate()
    return tree


def load_tree(path: Path | str) -> LogicTree:
    path = Path(path)
    return tree_from_dict(yaml.safe_load(path.read_text()) or {}, path)


def load_all_trees(root: Path | str = LOGIC_DIR) -> dict[str, LogicTree]:
    trees = {}
    for p in sorted(Path(root).glob("*.yaml")):
        t = load_tree(p)
        trees[t.engine] = t
    return trees


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def _holds(when: dict[str, Any] | None, var: str, vars_: dict[str, Any]) -> bool:
    if when is None:
        return True
    name = when.get("var", var)
    v = vars_.get(name)
    for op, val in when.items():
        if op == "var":
            continue
        if op == "missing":
            if (v is None) != bool(val):
                return False
            continue
        if op == "same_as":
            if v is None or v != vars_.get(val):
                return False
            continue
        if op == "differs_from":
            if v is None or vars_.get(val) is None or v == vars_.get(val):
                return False
            continue
        if v is None:  # every other operator needs a value
            return False
        if op == "in" and v not in val:
            return False
        if op == "not_in" and v in val:
            return False
        if op == "eq" and v != val:
            return False
        if op == "ne" and v == val:
            return False
        if op in ("lt", "le", "gt", "ge"):
            try:
                if op == "lt" and not v < val:
                    return False
                if op == "le" and not v <= val:
                    return False
                if op == "gt" and not v > val:
                    return False
                if op == "ge" and not v >= val:
                    return False
            except TypeError:
                return False
        if op not in ("in", "not_in", "eq", "ne", "lt", "le", "gt", "ge"):
            raise LogicError(f"unknown condition operator '{op}'")
    return True


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


@dataclass
class Walk:
    """What happened when a tree was walked with one set of inputs."""

    engine: str
    visited: list[str] = field(default_factory=list)
    taken_edges: list[tuple[str, str]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    vars: dict[str, Any] = field(default_factory=dict)
    status: Status = "ok"
    flags: list[str] = field(default_factory=list)
    blocked_on: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)


def _resolve_value(spec: Any, vars_: dict[str, Any]) -> Any:
    """A `result:` value of the form ``{var: name}`` copies another variable; else literal."""
    if isinstance(spec, dict) and set(spec) == {"var"}:
        return vars_.get(spec["var"])
    return spec


def _resolve(spec: Any, vars_: dict[str, Any]) -> Any:
    """A `with:` value is a variable name if it names one, else a literal."""
    if isinstance(spec, str) and spec in vars_:
        return vars_[spec]
    return spec


def walk(tree: LogicTree, inputs: dict[str, Any], tables: TableSet, trace: Trace | None = None) -> Walk:
    """Walk ``tree`` from its start node with ``inputs``, recording each node into ``trace``.

    The engine is expected to have opened a ``trace.section(...)`` so the node steps nest
    under it. Lookups go through ``tables``; a table that is not loaded produces a
    ``blocked`` (or ``not_supported``) step carrying the table's citation.
    """
    w = Walk(engine=tree.engine, vars=dict(inputs))
    node: Node | None = tree.node(tree.start)
    visits = 0
    while node is not None:
        visits += 1
        if visits > _MAX_VISITS:
            raise LogicError(f"{tree.engine}: more than {_MAX_VISITS} node visits – loop?")
        w.visited.append(node.id)
        step = Step(engine=tree.engine, title=node.label, node_id=node.id, citation=node.citation, rule=node.rule)
        nxt: str | None = None

        if node.kind == "decision":
            step.kind = "decision"
            step.inputs = {node.ask: w.vars.get(node.ask)}  # type: ignore[dict-item]
            taken = None
            for b in node.branches:
                if _holds(b.when, node.ask or "", w.vars):
                    taken = b
                    break
            if taken is None:
                step.status = "blocked"
                step.rule = (step.rule + " " if step.rule else "") + f"No branch covers {node.ask}={w.vars.get(node.ask)!r}."
            else:
                step.result = {"branch": taken.describe(node.ask or "")}
                if node.status:
                    step.status = node.status
                nxt = taken.goto

        elif node.kind == "lookup":
            step.kind = "lookup"
            args = {k: _resolve(v, w.vars) for k, v in node.with_.items()}
            step.inputs = args
            step.citation = node.citation or tables.citation(node.lookup or "")
            try:
                out = tables.lookup(node.lookup or "", **args)
                step.result = dict(out)
                w.vars.update(out)
                w.results[node.into or node.id] = out
                if node.status:
                    step.status = node.status
            except (TableNotLoaded, TableMissing) as e:
                step.status = "not_supported" if node.on_not_loaded == "not_supported" else "blocked"
                step.citation = e.citation
                step.rule = (step.rule + " " if step.rule else "") + (
                    f"{e.citation} – not loaded; values must be transcribed from the licensed standard before this step can be verified."
                    if isinstance(e, TableNotLoaded)
                    else f"No table file for '{e.table_id}'."
                )
                if step.status == "blocked":
                    w.blocked_on.append(e.citation)
            except OutOfRange as e:
                step.status = "blocked"
                step.citation = e.citation
                step.rule = (step.rule + " " if step.rule else "") + f"Outside tabulated range: {e.detail or e.inputs}. Engineer to design."
                w.blocked_on.append(f"{e.citation} (out of range)")
            nxt = node.goto

        else:  # result / end
            step.kind = "step"
            if node.result:
                resolved = {k: _resolve_value(v, w.vars) for k, v in node.result.items()}
                step.result = dict(resolved)
                w.results.update(resolved)
                w.vars.update(resolved)
            if node.preference:
                step.preference_applied = node.preference
            if node.status:
                step.status = node.status
            if node.flag:
                step.rule = (step.rule + " " if step.rule else "") + node.flag
                w.flags.append(node.flag)
            if step.status == "blocked" and step.citation:
                w.blocked_on.append(step.citation)
            nxt = None if node.end else node.goto

        if trace is not None:
            trace.add(step)
        w.steps.append(step)
        w.status = worst(w.status, step.status)
        if nxt is not None:
            w.taken_edges.append((node.id, nxt))
            node = tree.node(nxt)
        else:
            node = None
    return w


# ---------------------------------------------------------------------------
# Mermaid rendering of the whole tree, optionally with a walk highlighted
# ---------------------------------------------------------------------------


def _esc(text: str) -> str:
    return (
        str(text)
        .replace('"', "'")
        .replace("[", "(")
        .replace("]", ")")
        .replace("{", "(")
        .replace("}", ")")
        .replace("|", "/")
        .replace("\n", " ")
    )


def _shape(node: Node, label: str) -> str:
    q = '"' + label + '"'
    if node.kind == "decision":
        return "{" + q + "}"
    if node.kind == "lookup":
        return "[[" + q + "]]"
    if node.kind == "end":
        return "([" + q + "])"
    return "(" + q + ")"


def tree_to_mermaid(tree: LogicTree, walk_: Walk | None = None, tables: TableSet | None = None) -> str:
    """Render the entire tree. With a ``walk_`` the taken path is thick/coloured and the
    branches not taken are dotted grey, so the engineer sees where the job went."""
    taken_nodes = set(walk_.visited) if walk_ else set()
    taken_edges = set(walk_.taken_edges) if walk_ else set()
    node_status = {s.node_id: s.status for s in walk_.steps} if walk_ else {}

    out = [f"flowchart TD", f"  %% {tree.engine}: {_esc(tree.title)}"]
    out += [
        "  classDef taken stroke-width:3px,stroke:#1565c0",
        "  classDef nottaken stroke:#b0bec5,color:#78909c,fill:#fafafa",
        "  classDef ok fill:#e8f5e9,stroke:#2e7d32,stroke-width:3px",
        "  classDef flag fill:#fff8e1,stroke:#f9a825,stroke-width:3px",
        "  classDef blocked fill:#ffebee,stroke:#c62828,stroke-width:3px",
        "  classDef not_supported fill:#eceff1,stroke:#607d8b,stroke-width:3px",
        "  classDef lookup_unloaded stroke-dasharray:4 3",
    ]
    for n in tree.nodes.values():
        label = _esc(n.label)
        if n.citation:
            label += f"<br/><i>{_esc(n.citation)}</i>"
        if n.kind == "lookup" and tables is not None and n.lookup:
            loaded = n.lookup in tables and tables.get(n.lookup).loaded
            label += "<br/>" + ("table loaded" if loaded else "table NOT loaded")
        out.append(f"  {n.id}{_shape(n, label)}")

    link_styles = []
    for i, (a, b, lbl) in enumerate(tree.edges()):
        if walk_ is None:
            arrow = f"-->|{_esc(lbl)}|" if lbl else "-->"
        elif (a, b) in taken_edges:
            arrow = f"==>|{_esc(lbl)}|" if lbl else "==>"
        else:
            arrow = f"-.->|{_esc(lbl)}|" if lbl else "-.->"
            link_styles.append(f"  linkStyle {i} stroke:#b0bec5,color:#78909c")
        out.append(f"  {a} {arrow} {b}")
    out += link_styles

    for n in tree.nodes.values():
        classes = []
        if walk_ is not None:
            if n.id in taken_nodes:
                classes.append(node_status.get(n.id, "ok"))
            else:
                classes.append("nottaken")
        if n.kind == "lookup" and tables is not None and n.lookup and not (n.lookup in tables and tables.get(n.lookup).loaded):
            classes.append("lookup_unloaded")
        if classes:
            out.append(f"  class {n.id} {','.join(classes)}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Helper for engines that look tables up directly (outside a tree)
# ---------------------------------------------------------------------------


def lookup_step(trace: Trace, tables: TableSet, engine: str, table_id: str, title: str = "", **inputs: Any) -> tuple[dict[str, Any] | None, Step]:
    """Look a table up and record the attempt. Returns (outputs or None, step)."""
    citation = tables.citation(table_id)
    step = Step(engine=engine, kind="lookup", title=title or citation, citation=citation, inputs=dict(inputs))
    try:
        out = tables.lookup(table_id, **inputs)
        step.result = dict(out)
        trace.add(step)
        return out, step
    except TableNotLoaded as e:
        step.status = "blocked"
        step.citation = e.citation
        step.rule = f"{e.citation} – not loaded; transcribe from the licensed standard to verify."
    except TableMissing as e:
        step.status = "blocked"
        step.rule = f"No table file for '{e.table_id}'."
    except OutOfRange as e:
        step.status = "blocked"
        step.citation = e.citation
        step.rule = f"Outside tabulated range: {e.detail or e.inputs}. Engineer to design."
    except TableError as e:  # pragma: no cover – defensive
        step.status = "blocked"
        step.rule = str(e)
    trace.add(step)
    return None, step
