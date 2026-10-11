"""Which reference-product (originator) documents to process, and for what.

Reference documents are not searched for their own sake; they serve three purposes for
biosimilar questions:
  baseline_trials     the originator's pivotal trials (equivalence margins, study populations)
  indication_history  when and on what evidence each indication / dosing route was approved
                      (the conditions of use a biosimilar extrapolates to)
  reference_label     the current originator label (biosimilar labels are compared with it)

EMA variation assessment reports are kept only when the EMA "procedural steps taken after
authorisation" record shows the procedure added or changed an indication, strength, form or
route; other variations (paediatric study reports under Art. 46, DDI updates) are left out.
Rules only, no LLM.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

PURPOSES = ("baseline_trials", "indication_history", "reference_label")

_PROC_ID = re.compile(r"(?:EMA/)?(VR/\d{10}|(?:II|X|P46|IB|IA|IAIN|R|T|N)/\d{3,5}(?:/G)?)")
_INDICATION_SCOPE = re.compile(
    r"extension of indication|C\.I\.6|new therapeutic indication|line extension|"
    r"new (?:pharmaceutical form|strength|route)|addition of a new (?:strength|pharmaceutical form|route)", re.I)
_TITLE_PROC = re.compile(r"-(II|X|P46)-0*(\d+)(-G)?\b|(VR)-(\d{10})")


def procedure_scopes(blocks: list[dict]) -> dict[str, str]:
    """Procedure number -> scope text, from the tables of a procedural-steps document.
    A table row that starts a new procedure number opens a record; continuation rows
    (a scope split over rows) are appended to it."""
    scopes: dict[str, str] = {}
    cur = None
    for b in blocks:
        if b["type"] != "table":
            continue
        rows: dict[int, list[str]] = {}
        for c in b["cells"]:
            rows.setdefault(c["r"], []).append(c["text"])
        for _, cells in sorted(rows.items()):
            line = " | ".join(cells)
            ids = _PROC_ID.findall(line)
            if ids:
                cur = ids[0]
                scopes.setdefault(cur, "")
            if cur:
                scopes[cur] += " " + line
    return scopes


def title_procedure(title: str) -> str | None:
    """'Stelara-H-C-958-II-0029 : EPAR - ...' -> 'II/0029'; initial EPAR -> None."""
    m = _TITLE_PROC.search(title)
    if not m:
        return None
    if m.group(4):
        return f"VR/{m.group(5)}"
    kind, num = m.group(1), int(m.group(2))
    if kind == "P46":
        return f"P46/{num:03d}"
    return f"{kind}/{num:04d}" + ("/G" if m.group(3) else "")


def reference_plan(s: Session, program_id: str, procedural_blocks: list[list[dict]]) -> list[dict]:
    """Documents of a reference program with the purposes they serve; purpose-less documents
    are listed with an empty purpose list and a reason (they are not processed)."""
    from ra_ingest.db import DocumentRow

    scopes: dict[str, str] = {}
    for blocks in procedural_blocks:
        scopes.update(procedure_scopes(blocks))
    plan = []
    for d in s.scalars(select(DocumentRow).where(DocumentRow.program_id == program_id)):
        purposes: list[str] = []
        reason = ""
        t = d.doc_type
        if t == "assessment_report":
            proc = title_procedure(d.title or "")
            if proc is None:
                purposes, reason = ["baseline_trials", "indication_history"], "initial assessment report"
            elif _INDICATION_SCOPE.search(scopes.get(proc, "")):
                purposes, reason = ["indication_history"], f"{proc}: indication/form/route change"
            else:
                reason = f"{proc}: not an indication change" + ("" if proc in scopes else " (not in history)")
        elif t in ("review", "sbd"):
            purposes, reason = ["baseline_trials", "indication_history"], "agency review of an application"
        elif t in ("approval_letter", "rds", "procedural_steps", "epar_overview"):
            purposes, reason = ["indication_history"], "approval record"
        elif t in ("label", "product_information", "product_monograph"):
            purposes, reason = ["reference_label"], "originator label (latest kept by selection)"
        else:
            reason = f"{t}: no reference purpose"
        plan.append({"doc_id": d.id, "agency": d.agency, "doc_type": t, "title": (d.title or "")[:90],
                     "purposes": purposes, "reason": reason})
    return sorted(plan, key=lambda x: (x["agency"], x["doc_type"], x["doc_id"]))


def load_blocks(path: str | Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["blocks"]
