"""Design-logic page: every engine's decision tree as a Mermaid flowchart plus the tables it
depends on and whether they are loaded. Plain HTML, no build step.

Routes (mount with ``app.include_router(router)`` in main.py):

* ``GET /design-logic``              – the page
* ``GET /design-logic/{engine}.mmd`` – raw Mermaid for one engine
* ``GET /tables/status``             – JSON: every standards table, loaded?, rows, checked by
"""

from __future__ import annotations

import html

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .engines.logic import LogicTree, load_all_trees, tree_to_mermaid
from .engines.tables import TableSet, default_tables, tables_status

router = APIRouter()

MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"

_CSS = """
body{font-family:system-ui,Segoe UI,Helvetica,Arial,sans-serif;margin:0;background:#f6f7f9;color:#1b1b1b}
header{background:#1f3a5f;color:#fff;padding:14px 24px}
header h1{margin:0;font-size:20px;font-weight:600}
header p{margin:4px 0 0;opacity:.8;font-size:13px}
main{max-width:1200px;margin:0 auto;padding:16px 24px 48px}
nav a{margin-right:14px;font-size:14px}
section.engine{background:#fff;border:1px solid #dde1e6;border-radius:8px;padding:18px 22px;margin:18px 0}
section.engine h2{margin:0 0 4px;font-size:18px}
.std{color:#555;font-size:13px;margin:0 0 10px}
.desc{font-size:14px;color:#333;margin:0 0 12px}
table{border-collapse:collapse;font-size:13px;margin:8px 0 14px}
th,td{border:1px solid #dde1e6;padding:4px 8px;text-align:left}
th{background:#eef1f5}
.ok{color:#2e7d32;font-weight:600}.no{color:#c62828;font-weight:600}
pre.mermaid{background:#fafbfc;border:1px dashed #cfd5dc;border-radius:6px;padding:10px;overflow:auto}
code{background:#eef1f5;padding:1px 4px;border-radius:3px}
.legend{font-size:12px;color:#555;margin:6px 0 0}
details summary{cursor:pointer;font-size:13px;color:#1f3a5f}
"""


def _tables_for(tree: LogicTree, tables: TableSet) -> list[dict]:
    rows = []
    for tid in tree.tables:
        if tid in tables:
            t = tables.get(tid)
            rows.append({"id": tid, "citation": t.citation, "title": t.title, "loaded": t.loaded, "rows": len(t.rows), "checked": t.source_checked_by})
        else:
            rows.append({"id": tid, "citation": tid, "title": "(no file)", "loaded": False, "rows": 0, "checked": ""})
    return rows


def _engine_section(tree: LogicTree, tables: TableSet) -> str:
    e = html.escape
    trows = "".join(
        f"<tr><td><code>{e(r['id'])}</code></td><td>{e(r['citation'])}</td><td>{e(r['title'])}</td>"
        f"<td class='{'ok' if r['loaded'] else 'no'}'>{'loaded (' + str(r['rows']) + ' rows)' if r['loaded'] else 'not loaded'}</td>"
        f"<td>{e(r['checked']) or '–'}</td></tr>"
        for r in _tables_for(tree, tables)
    )
    mermaid = tree_to_mermaid(tree, tables=tables)
    src = e(tree.path.name) if tree.path else ""
    return f"""
<section class="engine" id="{e(tree.engine)}">
  <h2>{e(tree.title)}</h2>
  <p class="std">Engine <code>{e(tree.engine)}</code> · {e(tree.standard)} · logic file <code>app/logic/{src}</code> ·
     <a href="/design-logic/{e(tree.engine)}.mmd">raw Mermaid</a></p>
  <p class="desc">{e(tree.description.strip())}</p>
  <details open><summary>Decision tree</summary>
  <pre class="mermaid">{e(mermaid)}</pre>
  <p class="legend">Diamonds are decisions, double-bordered boxes are table lookups (dashed border = table not loaded),
     rounded boxes are results, pills are end points. Inputs: {e(', '.join(tree.inputs))}.</p>
  </details>
  <h3 style="font-size:15px;margin:14px 0 4px">Tables this engine depends on</h3>
  <table><tr><th>Table id</th><th>Citation</th><th>Title</th><th>Status</th><th>Checked by</th></tr>{trows}</table>
</section>"""


def render_page(trees: dict[str, LogicTree], tables: TableSet) -> str:
    e = html.escape
    status = tables.status()
    loaded = sum(1 for s in status if s["loaded"])
    nav = " ".join(f'<a href="#{e(t.engine)}">{e(t.title)}</a>' for t in trees.values())
    sections = "".join(_engine_section(t, tables) for t in trees.values())
    all_rows = "".join(
        f"<tr><td><code>{e(s['id'])}</code></td><td>{e(s['citation'])}</td><td>{e(s['title'])}</td>"
        f"<td class='{'ok' if s['loaded'] else 'no'}'>{'loaded (' + str(s['rows']) + ')' if s['loaded'] else 'not loaded'}</td>"
        f"<td>{e(s['source_checked_by']) or '–'}</td></tr>"
        for s in status
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Design logic</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_CSS}</style></head>
<body>
<header><h1>Design logic</h1>
<p>Deterministic engines – every branch below is data in <code>app/logic/</code>; every number comes from a standards
table in <code>app/data/standards/</code>. {loaded} of {len(status)} tables loaded. <a style="color:#cfe3ff" href="/tables/status">tables/status JSON</a></p></header>
<main>
<nav>{nav} <a href="#tables">All tables</a></nav>
{sections}
<section class="engine" id="tables">
  <h2>All standards tables</h2>
  <p class="desc">Values are transcribed from the practice's licensed copies and checked by an engineer (initials + date in
  <code>source_checked_by</code>). An unloaded table makes the engine report <em>blocked</em> with the citation rather than guess.</p>
  <table><tr><th>Table id</th><th>Citation</th><th>Title</th><th>Status</th><th>Checked by</th></tr>{all_rows}</table>
</section>
</main>
<script src="{MERMAID_CDN}"></script>
<script>mermaid.initialize({{startOnLoad:true, securityLevel:'loose', theme:'neutral', flowchart:{{htmlLabels:true, curve:'basis'}}}});</script>
</body></html>"""


@router.get("/design-logic", response_class=HTMLResponse)
def design_logic() -> str:
    return render_page(load_all_trees(), default_tables())


@router.get("/design-logic/{engine}.mmd", response_class=PlainTextResponse)
def design_logic_mermaid(engine: str) -> str:
    trees = load_all_trees()
    if engine not in trees:
        raise HTTPException(404, f"No engine '{engine}'. Known: {sorted(trees)}")
    return tree_to_mermaid(trees[engine], tables=default_tables())


@router.get("/tables/status")
def tables_status_json() -> JSONResponse:
    status = tables_status()
    return JSONResponse({"loaded": sum(1 for s in status if s["loaded"]), "total": len(status), "tables": status})
