"""Execute a DesignPlan: run every active step's engine and collect one trace for the job."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..engines import ENGINES
from ..engines.prefs import load_preferences
from ..engines.tables import TableSet, default_tables
from ..engines.trace import Trace
from ..model import Project
from .planner import DesignPlan, plan

# Engines the planner can schedule but that have no implementation yet. Each run records a
# blocked step naming what it needs, so the gap is visible in the trace rather than silent.
PENDING: dict[str, str] = {
    "site_as2870": "AS 2870:2011 Table 2.3 cross-check of class vs ys (implemented inside footings engine for now)",
    "racking_as1684": "AS 1684.2:2021 Tables 8.1–8.3 (racking pressure) and Table 8.18 (bracing capacities)",
    "lintels_as1684": "AS 1684.2 Supplement lintel span tables (F17, N1/N2 and N3/N4)",
    "floor_members_as1684": "AS 1684.2 Supplement bearer and floor joist tables (F7 / F17)",
    "tie_down_as1684": "Practice Table 4 tie-down variants / AS 1684.2 Section 9",
    "articulation_as4773": "AS 4773.2 / AS 2870 App. C articulation joint spacing",
    "roof_members_as1684": "AS 1684.2 Supplement rafter / ridge / underpurlin tables",
    "deck_members_as1684": "AS 1684.2 Supplement deck bearer / joist tables (F7 TP)",
    "steel_as4100": "AS 4100 engine wrapper over app/calc/steel.py",
    "load_path": "load-path assembly from member reactions (footings engine records the simple case)",
    "piers_pads": "pier / pad sizing from column reactions and bearing capacity",
}


@dataclass
class DesignRun:
    plan: DesignPlan
    trace: Trace
    results: dict[str, Any] = field(default_factory=dict)  # step id -> engine Result
    errors: dict[str, str] = field(default_factory=dict)  # step id -> exception text

    def result(self, engine: str, scope_prefix: str = "") -> Any | None:
        """First result for `engine` whose plan step scope starts with `scope_prefix`."""
        for s in self.plan.steps:
            if s.engine == engine and s.scope.startswith(scope_prefix) and s.id in self.results:
                return self.results[s.id]
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.trace.status,
            "steps_run": len(self.results),
            "steps_skipped": len([s for s in self.plan.steps if s.skipped]),
            "blocked_on": self.trace.blocked_on(),
            "flags": self.trace.flags(),
            "errors": self.errors,
        }


def run_design(
    project: Project,
    tables: TableSet | None = None,
    prefs: dict | None = None,
    design_plan: DesignPlan | None = None,
) -> DesignRun:
    """Deterministic: same project + tables + prefs → same trace."""
    tables = tables or default_tables()
    prefs = prefs or load_preferences()
    design_plan = design_plan or plan(project, prefs)
    trace = Trace(project_name=project.project_name, job_no=project.job_no)
    run = DesignRun(plan=design_plan, trace=trace)

    with trace.section("planner", "Design process", rule="Steps in dependency order; skipped steps keep their reason."):
        for s in design_plan.steps:
            trace.add(_plan_step(s))

    for step in design_plan.active():
        building_id = step.scope.split("/")[0] if step.scope != "site" else (project.buildings[0].id if project.buildings else "")
        mod = ENGINES.get(step.engine)
        if mod is None:
            trace.step(step.engine, step.title, status="blocked", citation=PENDING.get(step.engine, "engine not implemented"),
                       rule=f"Engine '{step.engine}' is not implemented yet; scope {step.scope}.", inputs={"scope": step.scope, **step.params})
            continue
        try:
            run.results[step.id] = mod.run(project, building_id, tables, prefs, trace)
        except Exception as e:  # one failing engine must not lose the rest of the job
            run.errors[step.id] = f"{type(e).__name__}: {e}"
            trace.step(step.engine, step.title, status="blocked", rule=f"Engine raised {type(e).__name__}: {e}", inputs={"scope": step.scope})
    return run


def _plan_step(s):  # small helper so the plan is visible at the top of the trace
    from ..engines.trace import Step

    return Step(engine="planner", title=s.title, kind="scope", inputs={"scope": s.scope, "needs": s.needs, **s.params},
                rule=s.reason, status="not_supported" if s.skipped else "ok", result={"skipped": s.skipped} if s.skipped else {})
