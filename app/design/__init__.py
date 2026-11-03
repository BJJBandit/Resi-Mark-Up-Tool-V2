"""Design stage: deterministic, token-free.

`planner` turns a reviewed `Project` into an ordered list of engine runs (the design
process as a dependency graph) and the sheet set for each building. `runner` (added with
the engines) executes the plan, collecting one trace per job.
"""

from .planner import DesignPlan, PlanStep, plan  # noqa: F401
