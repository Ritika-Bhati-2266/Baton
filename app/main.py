from __future__ import annotations

import io
import json
import os
import re
import urllib.parse
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db as database
from app.db import audit, execute, init_db, new_id, now, query, query_one
from app.pipeline import build_case
from app.redaction import redaction_preview
from app.seed import CASE_ID, seed_demo
from app.verify import (
    LABEL_HELP,
    LABEL_ORDER,
    ROLE_ACLS,
    claims_with_evidence,
    handover_claims,
    normalize,
    publish_rules,
)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

ROLE_NAMES = {
    "approver": "Approver",
    "departing": "Departing member",
    "incoming": "Incoming person",
    "admin": "Admin",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    seed_demo()
    if not query_one("SELECT COUNT(*) AS n FROM claims WHERE case_id = ?", (CASE_ID,))["n"]:
        if query_one("SELECT COUNT(*) AS n FROM sources WHERE case_id = ?", (CASE_ID,))["n"]:
            build_case(CASE_ID)
    yield


app = FastAPI(
    title="Baton",
    description="Evidence-linked, permission-aware knowledge handover",
    version="0.2.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def redirect(url: str, msg: str | None = None, kind: str = "ok") -> RedirectResponse:
    if msg:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode({'msg': msg, 'kind': kind})}"
    return RedirectResponse(url, status_code=303)


def get_case(case_id: str) -> dict:
    case = query_one("SELECT * FROM cases WHERE id = ?", (case_id,))
    if not case:
        raise HTTPException(status_code=404, detail="Handover case not found")
    return case


def actor_for(case: dict, role: str = "approver") -> str:
    return {
        "approver": case.get("approver_name") or "Approver",
        "incoming": case.get("incoming_name") or "Incoming person",
        "departing": case.get("departing_name") or "Departing member",
        "admin": "Admin",
    }.get(role, case.get("approver_name") or "Approver")


def review_url(case_id: str) -> str:
    return f"/cases/{case_id}/review"


def clickable_sources(case_id: str) -> list[dict]:
    sources = query("SELECT * FROM sources WHERE case_id = ? ORDER BY created_at", (case_id,))
    for source in sources:
        source["chunk_count"] = query_one(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_id = ?", (source["id"],)
        )["n"]
        source["preview"] = redaction_preview(source["content"])
    return sources


def claim_counts(case_id: str) -> dict:
    rows = query(
        "SELECT evidence_label AS label, COUNT(*) AS n FROM claims WHERE case_id = ? GROUP BY evidence_label",
        (case_id,),
    )
    counts = {row["label"]: row["n"] for row in rows}
    counts["TOTAL"] = sum(counts.values())
    counts["approved"] = query_one(
        "SELECT COUNT(*) AS n FROM claims WHERE case_id = ? AND status = 'approved'", (case_id,)
    )["n"]
    counts["pending"] = query_one(
        "SELECT COUNT(*) AS n FROM claims WHERE case_id = ? AND status = 'pending'", (case_id,)
    )["n"]
    counts["rejected"] = query_one(
        "SELECT COUNT(*) AS n FROM claims WHERE case_id = ? AND status = 'rejected'", (case_id,)
    )["n"]
    return counts


def dedupe_next_actions(claims: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    order: list[str] = []
    for claim in claims:
        key = claim.get("topic_key") or claim["id"]
        if key not in best:
            best[key] = claim
            order.append(key)
            continue
        old = best[key]
        old_score = (bool(old.get("owner")), bool(old.get("due_date")))
        new_score = (bool(claim.get("owner")), bool(claim.get("due_date")))
        if new_score > old_score:
            best[key] = claim
    return [best[key] for key in order]


def build_summary(case: dict, groups: dict[str, list[dict]], gaps: list[dict]) -> list[str]:
    parts = [
        f"You are taking over {case.get('project') or case['title']} from {case.get('departing_name') or 'the departing member'}."
    ]
    decisions = groups.get("decision", [])
    if decisions:
        parts.append(f"Current direction: {decisions[0]['text']}")
    risks = groups.get("risk", []) or groups.get("blocker", [])
    if risks:
        parts.append(f"Top risk: {risks[0]['text']}")
    actions = groups.get("next_action", [])
    if actions:
        parts.append(f"First action: {actions[0]['text']}")
    if gaps:
        parts.append(f"Known gaps: {len(gaps)}. They are listed with the evidence trail below.")
    return parts[:5]


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cases = query("SELECT * FROM cases ORDER BY created_at DESC")
    for case in cases:
        case["source_count"] = query_one(
            "SELECT COUNT(*) AS n FROM sources WHERE case_id = ?", (case["id"],)
        )["n"]
        case["claim_count"] = query_one(
            "SELECT COUNT(*) AS n FROM claims WHERE case_id = ?", (case["id"],)
        )["n"]
        case["approved_count"] = query_one(
            "SELECT COUNT(*) AS n FROM claims WHERE case_id = ? AND status = 'approved'",
            (case["id"],),
        )["n"]
        case["open_conflicts"] = query_one(
            "SELECT COUNT(*) AS n FROM conflicts WHERE case_id = ? AND status = 'open'",
            (case["id"],),
        )["n"]
        case["open_gaps"] = query_one(
            """SELECT COUNT(*) AS n FROM claims
               WHERE case_id = ? AND is_gap = 1 AND gap_critical = 1 AND gap_accepted = 0""",
            (case["id"],),
        )["n"]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"cases": cases, "demo_id": CASE_ID, "labels": LABEL_ORDER, "label_help": LABEL_HELP},
    )


@app.post("/cases")
def create_case(
    project: str = Form(...),
    departing: str = Form(...),
    incoming: str = Form(...),
    approver: str = Form(...),
):
    case_id = new_id("case")
    project = project.strip() or "Project"
    departing = departing.strip() or "Departing member"
    incoming = incoming.strip() or "Incoming person"
    approver = approver.strip() or "Approver"
    execute(
        """INSERT INTO cases
           (id, title, project, departing_name, incoming_name, approver_name, status, version, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            case_id,
            f"{project} handover: {departing} to {incoming}",
            project,
            departing,
            incoming,
            approver,
            "draft",
            1,
            now(),
        ),
    )
    audit(case_id, approver, "approver", "case_created", f"{project}: {departing} to {incoming}")
    return redirect(f"/cases/{case_id}/sources", "Case created. Add authorised sources, then run the pipeline.")


@app.get("/cases/{case_id}/review", response_class=HTMLResponse)
def review(
    request: Request,
    case_id: str,
    label: str = "",
    ctype: str = "",
    status: str = "",
    q: str = "",
):
    case = get_case(case_id)
    claims = claims_with_evidence(case_id)
    gaps = [c for c in claims if c["is_gap"]]
    normal = [c for c in claims if not c["is_gap"]]

    if label:
        normal = [c for c in normal if c["evidence_label"] == label]
    if ctype:
        normal = [c for c in normal if c["type"] == ctype]
    if status:
        normal = [c for c in normal if c["status"] == status]
    if q:
        needle = q.lower()
        normal = [
            c for c in normal
            if needle in (c["text"] or "").lower()
            or any(needle in (e.get("quote") or "").lower() for e in c.get("evidence", []))
        ]

    conflicts = query(
        "SELECT * FROM conflicts WHERE case_id = ? ORDER BY (status = 'resolved'), created_at",
        (case_id,),
    )
    for conflict in conflicts:
        conflict["a"] = query_one("SELECT * FROM claims WHERE id = ?", (conflict["claim_a"],))
        conflict["b"] = query_one("SELECT * FROM claims WHERE id = ?", (conflict["claim_b"],))
        group = query(
            """SELECT * FROM claims
               WHERE case_id = ? AND topic_key = ? AND is_gap = 0
                 AND type IN ('decision', 'owner')
               ORDER BY created_at, id""",
            (case_id, conflict["topic_key"]),
        )
        for claim in group:
            claim["evidence"] = query(
                """SELECT e.*, s.name AS source_name, s.source_date AS source_date, s.kind AS source_kind
                   FROM evidence e LEFT JOIN sources s ON s.id = e.source_id
                   WHERE e.claim_id = ? ORDER BY e.rowid""",
                (claim["id"],),
            )
        conflict["group"] = group
        value_groups: dict[str, list[dict]] = {}
        for claim in group:
            key = normalize(claim["value"] or claim["text"])
            value_groups.setdefault(key, []).append(claim)
        conflict["value_groups"] = list(value_groups.values())

    rules = publish_rules(case_id)
    counts = claim_counts(case_id)
    sources = query("SELECT id, name, kind, acl FROM sources WHERE case_id = ?", (case_id,))
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "case": case,
            "claims": normal,
            "gaps": gaps,
            "conflicts": conflicts,
            "rules": rules,
            "counts": counts,
            "sources": sources,
            "labels": LABEL_ORDER,
            "label_help": LABEL_HELP,
            "claim_types": [
                "decision", "owner", "risk", "dependency", "open_question",
                "next_action", "blocker", "recurring_problem", "contact",
            ],
            "filters": {"label": label, "ctype": ctype, "status": status, "q": q},
            "msg": request.query_params.get("msg"),
            "msg_kind": request.query_params.get("kind", "ok"),
        },
    )


@app.get("/cases/{case_id}/sources", response_class=HTMLResponse)
def sources_page(request: Request, case_id: str):
    case = get_case(case_id)
    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "case": case,
            "sources": clickable_sources(case_id),
            "msg": request.query_params.get("msg"),
            "msg_kind": request.query_params.get("kind", "ok"),
        },
    )


@app.get("/cases/{case_id}/audit", response_class=HTMLResponse)
def audit_page(request: Request, case_id: str):
    case = get_case(case_id)
    entries = query(
        "SELECT * FROM audit WHERE case_id = ? ORDER BY id DESC LIMIT 200", (case_id,)
    )
    return templates.TemplateResponse(
        request, "audit.html", {"case": case, "entries": entries}
    )


@app.get("/cases/{case_id}/handover", response_class=HTMLResponse)
def handover(request: Request, case_id: str, role: str = "incoming"):
    case = get_case(case_id)
    if role not in ROLE_ACLS:
        role = "incoming"
    if case["status"] != "published" and role != "approver":
        return templates.TemplateResponse(
            request,
            "handover_gate.html",
            {"case": case, "role": role},
        )

    data = handover_claims(case_id, role)
    visible = data["visible"]
    gaps = [c for c in visible if c["is_gap"]]
    normal = [c for c in visible if not c["is_gap"]]
    groups: dict[str, list[dict]] = defaultdict(list)
    for claim in normal:
        groups[claim["type"]].append(claim)

    groups["next_action"] = dedupe_next_actions(groups.get("next_action", []))
    groups["decision"] = sorted(
        groups.get("decision", []), key=lambda c: (c.get("event_date") or "", c["created_at"])
    )
    groups["next_action"] = sorted(
        groups.get("next_action", []), key=lambda c: (c.get("due_date") or "9999-12-31", c["created_at"])
    )

    incoming_name = case.get("incoming_name") or ""
    owned = [
        c for c in groups.get("next_action", [])
        if normalize(c.get("owner")) == normalize(incoming_name)
    ]
    summary = build_summary(case, groups, gaps)

    return templates.TemplateResponse(
        request,
        "handover.html",
        {
            "case": case,
            "role": role,
            "role_name": ROLE_NAMES.get(role, role),
            "allowed_acls": sorted(ROLE_ACLS.get(role, {"team"})),
            "groups": groups,
            "gaps": gaps,
            "owned": owned,
            "summary": summary,
            "hidden_count": data["hidden_count"],
            "labels": LABEL_ORDER,
            "label_help": LABEL_HELP,
            "msg": request.query_params.get("msg"),
            "msg_kind": request.query_params.get("kind", "ok"),
        },
    )


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

@app.post("/cases/{case_id}/build")
def run_pipeline(case_id: str):
    get_case(case_id)
    result = build_case(case_id)
    if result["ok"]:
        return redirect(
            review_url(case_id),
            f"Pipeline complete: {result['total_claims']} claims from {result['sources']} sources "
            f"({result['claims']} extracted + {result['gaps']} gap(s)), "
            f"{result['conflicts']} conflict(s).",
        )
    return redirect(f"/cases/{case_id}/sources", result["error"], "error")


@app.post("/claims/{claim_id}/approve")
def approve_claim(claim_id: str):
    claim = query_one("SELECT * FROM claims WHERE id = ?", (claim_id,))
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    case = get_case(claim["case_id"])
    if claim["evidence_label"] in {"CONFLICTING", "QUOTE_FAILED"}:
        return redirect(
            review_url(case["id"]),
            "That claim cannot be approved while it is conflicting or its quote failed verification.",
            "error",
        )
    if claim["is_gap"] and claim["gap_critical"] and not claim["gap_accepted"]:
        return redirect(
            review_url(case["id"]),
            "Use 'Answer gap' or 'Accept gap' on the critical gap card.",
            "error",
        )
    actor = actor_for(case)
    execute(
        "UPDATE claims SET status = 'approved', reviewer = ?, reviewed_at = ? WHERE id = ?",
        (actor, now(), claim_id),
    )
    audit(case["id"], actor, "approver", "claim_approved", claim["text"][:180])
    return redirect(review_url(case["id"]), "Claim approved.")


@app.post("/claims/{claim_id}/reject")
def reject_claim(claim_id: str):
    claim = query_one("SELECT * FROM claims WHERE id = ?", (claim_id,))
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    case = get_case(claim["case_id"])
    actor = actor_for(case)
    execute(
        "UPDATE claims SET status = 'rejected', reviewer = ?, reviewed_at = ? WHERE id = ?",
        (actor, now(), claim_id),
    )
    audit(case["id"], actor, "approver", "claim_rejected", claim["text"][:180])
    return redirect(review_url(case["id"]), "Claim rejected.")


@app.post("/claims/{claim_id}/edit")
def edit_claim(
    claim_id: str,
    text: str = Form(...),
    claim_type: str = Form("decision"),
    value: str = Form(""),
    owner: str = Form(""),
    due_date: str = Form(""),
    contact: str = Form(""),
):
    claim = query_one("SELECT * FROM claims WHERE id = ?", (claim_id,))
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    case = get_case(claim["case_id"])
    actor = actor_for(case)
    new_text = text.strip()
    if not new_text:
        return redirect(review_url(case["id"]), "Claim text cannot be empty.", "error")
    execute(
        """UPDATE claims
           SET text = ?, type = ?, value = ?, owner = ?, due_date = ?, contact = ?,
               basis = 'human', base_label = 'HUMAN_ASSERTED', evidence_label = 'HUMAN_ASSERTED',
               status = 'pending', reviewer = ?, reviewed_at = ?
           WHERE id = ?""",
        (
            new_text,
            claim_type,
            value.strip() or new_text,
            owner.strip() or None,
            due_date.strip() or None,
            contact.strip() or None,
            actor,
            now(),
            claim_id,
        ),
    )
    audit(
        case["id"],
        actor,
        "approver",
        "claim_edited",
        f"From: {claim['text'][:120]} | To: {new_text[:120]}",
    )
    return redirect(review_url(case["id"]), "Claim edited and labelled HUMAN_ASSERTED. Approve it again when ready.")


@app.post("/claims/{claim_id}/accept-gap")
def accept_gap(claim_id: str):
    claim = query_one("SELECT * FROM claims WHERE id = ?", (claim_id,))
    if not claim or not claim["is_gap"]:
        raise HTTPException(status_code=404, detail="Gap not found")
    case = get_case(claim["case_id"])
    actor = actor_for(case)
    execute(
        """UPDATE claims
           SET gap_accepted = 1, status = 'approved', reviewer = ?, reviewed_at = ?
           WHERE id = ?""",
        (actor, now(), claim_id),
    )
    audit(case["id"], actor, "approver", "gap_accepted", claim["text"][:180])
    return redirect(review_url(case["id"]), "Gap accepted and logged as a known gap.")


@app.post("/claims/{claim_id}/answer-gap")
def answer_gap(claim_id: str, answer: str = Form(...)):
    claim = query_one("SELECT * FROM claims WHERE id = ?", (claim_id,))
    if not claim or not claim["is_gap"]:
        raise HTTPException(status_code=404, detail="Gap not found")
    case = get_case(claim["case_id"])
    actor = actor_for(case)
    answer = answer.strip()
    if not answer:
        return redirect(review_url(case["id"]), "Enter an answer before saving.", "error")
    execute(
        """UPDATE claims
           SET gap_answer = ?, gap_accepted = 1, basis = 'human',
               base_label = 'HUMAN_ASSERTED', evidence_label = 'HUMAN_ASSERTED',
               status = 'approved', reviewer = ?, reviewed_at = ?
           WHERE id = ?""",
        (answer, actor, now(), claim_id),
    )
    audit(case["id"], actor, "approver", "gap_answered", f"{claim['text'][:120]} | Answer: {answer[:160]}")
    return redirect(review_url(case["id"]), "Answer saved as HUMAN_ASSERTED evidence.")


@app.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(
    conflict_id: str,
    choice: str = Form(""),
    claim_id: str = Form(""),
):
    conflict = query_one("SELECT * FROM conflicts WHERE id = ?", (conflict_id,))
    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")
    case = get_case(conflict["case_id"])
    actor = actor_for(case)
    group = query(
        """SELECT * FROM claims
           WHERE case_id = ? AND topic_key = ? AND is_gap = 0
             AND type IN ('decision', 'owner')""",
        (conflict["case_id"], conflict["topic_key"]),
    )

    if choice == "reject_both":
        for claim in group:
            execute(
                """UPDATE claims SET status = 'rejected', evidence_label = COALESCE(base_label, evidence_label),
                   reviewer = ?, reviewed_at = ? WHERE id = ?""",
                (actor, now(), claim["id"]),
            )
        resolved_value = "Rejected both sources"
        resolved_claim_id = None
    else:
        chosen_id = claim_id
        if not chosen_id:
            chosen_id = conflict["claim_a"] if choice == "a" else conflict["claim_b"]
        chosen = query_one("SELECT * FROM claims WHERE id = ?", (chosen_id,))
        if not chosen:
            return redirect(review_url(case["id"]), "Choose a side to keep.", "error")
        chosen_value = normalize(chosen["value"] or chosen["text"])
        for claim in group:
            label = claim["base_label"] or "VERIFIED"
            if normalize(claim["value"] or claim["text"]) == chosen_value:
                execute(
                    """UPDATE claims SET status = 'approved', evidence_label = ?,
                       reviewer = ?, reviewed_at = ? WHERE id = ?""",
                    (label, actor, now(), claim["id"]),
                )
            else:
                execute(
                    """UPDATE claims SET status = 'rejected', evidence_label = ?,
                       reviewer = ?, reviewed_at = ? WHERE id = ?""",
                    (label, actor, now(), claim["id"]),
                )
        resolved_value = chosen["value"] or chosen["text"]
        resolved_claim_id = chosen_id

    execute(
        """UPDATE conflicts
           SET status = 'resolved', resolved_value = ?, resolved_claim_id = ?,
               resolved_by = ?, resolved_at = ?
           WHERE id = ?""",
        (resolved_value, resolved_claim_id, actor, now(), conflict_id),
    )
    audit(
        case["id"],
        actor,
        "approver",
        "conflict_resolved",
        f"{conflict['topic_key']}: {resolved_value[:160]}",
    )
    return redirect(review_url(case["id"]), "Conflict resolved. Matching claims were approved; the other side was rejected.")


@app.post("/cases/{case_id}/approve-all")
def approve_all(case_id: str):
    case = get_case(case_id)
    actor = actor_for(case)
    claims = query(
        """SELECT * FROM claims
           WHERE case_id = ? AND status = 'pending' AND is_gap = 0
             AND evidence_label IN ('VERIFIED', 'SINGLE_SOURCE', 'HUMAN_ASSERTED', 'INFERRED')""",
        (case_id,),
    )
    for claim in claims:
        execute(
            "UPDATE claims SET status = 'approved', reviewer = ?, reviewed_at = ? WHERE id = ?",
            (actor, now(), claim["id"]),
        )
    audit(case_id, actor, "approver", "claims_approved_bulk", f"{len(claims)} claims approved")
    return redirect(review_url(case_id), f"{len(claims)} claim(s) approved. Conflicts and gaps still need a decision.")


@app.post("/cases/{case_id}/publish")
def publish(case_id: str):
    case = get_case(case_id)
    rules = publish_rules(case_id)
    if not rules["can_publish"]:
        return redirect(review_url(case_id), " | ".join(rules["reasons"]), "error")
    actor = actor_for(case)
    version = case["version"] + 1 if case["published_at"] else case["version"]
    execute(
        """UPDATE cases
           SET status = 'published', version = ?, published_at = ?, published_by = ?,
               acknowledged_at = NULL, acknowledged_by = NULL
           WHERE id = ?""",
        (version, now(), actor, case_id),
    )
    audit(case_id, actor, "approver", "handover_published", f"v{version} published to {case['incoming_name']}")
    return redirect(
        f"/cases/{case_id}/handover?role=incoming",
        f"Published as v{version}. The incoming person now has a role-filtered handover.",
    )


@app.post("/cases/{case_id}/acknowledge")
def acknowledge(case_id: str, role: str = Form("incoming")):
    case = get_case(case_id)
    if case["status"] != "published":
        return redirect(review_url(case_id), "The handover must be published before it can be acknowledged.", "error")
    actor = actor_for(case, "incoming")
    execute(
        "UPDATE cases SET acknowledged_at = ?, acknowledged_by = ? WHERE id = ?",
        (now(), actor, case_id),
    )
    audit(case_id, actor, "incoming", "handover_acknowledged", f"Receipt acknowledged for v{case['version']}")
    return redirect(
        f"/cases/{case_id}/handover?role=incoming",
        "Receipt acknowledged. The approver can see this in the audit log.",
    )


# ---------------------------------------------------------------------------
# source ingestion
# ---------------------------------------------------------------------------

def _detect_kind(filename: str, content: str) -> str:
    low = filename.lower()
    if low.endswith(".csv"):
        return "task_csv"
    if low.endswith(".json"):
        return "repo"
    if "chat" in low or re.search(r"^\[\d{4}-\d{2}-\d{2}", content, re.MULTILINE):
        return "chat"
    if "meeting" in low or "notes" in low:
        return "notes"
    return "doc"


@app.post("/cases/{case_id}/sources/upload")
async def upload_source(
    case_id: str,
    file: UploadFile = File(...),
    acl: str = Form("team"),
    kind: str = Form("auto"),
    source_date: str = Form(""),
    author: str = Form(""),
):
    case = get_case(case_id)
    raw = await file.read()
    if len(raw) > 2_000_000:
        return redirect(f"/cases/{case_id}/sources", "File is larger than the 2 MB demo limit.", "error")
    filename = file.filename or "upload.txt"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    try:
        if ext == "pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw))
            content = "\n".join((page.extract_text() or "") for page in reader.pages)
        else:
            content = raw.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return redirect(f"/cases/{case_id}/sources", f"Could not read the file: {exc}", "error")

    if not content.strip():
        return redirect(f"/cases/{case_id}/sources", "No readable text found in that file.", "error")
    if acl not in {"team", "leads", "private"}:
        acl = "team"
    if kind == "auto":
        kind = _detect_kind(filename, content)
    if kind not in {"doc", "notes", "task_csv", "repo", "chat"}:
        kind = "doc"

    source_id = new_id("src")
    execute(
        """INSERT INTO sources
           (id, case_id, name, kind, acl, content, author, source_date, external_url, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            source_id,
            case_id,
            filename,
            kind,
            acl,
            content,
            author.strip() or None,
            source_date.strip() or None,
            None,
            now(),
        ),
    )
    audit(case_id, actor_for(case), "approver", "source_uploaded", f"{filename} ({kind}, {acl})")
    return redirect(
        f"/cases/{case_id}/sources",
        f"{filename} added as {kind} with {acl} access. Run the pipeline to include it.",
    )


@app.post("/cases/{case_id}/sources/github")
async def import_github(
    case_id: str,
    repo: str = Form(...),
    acl: str = Form("team"),
):
    case = get_case(case_id)
    repo = repo.strip()
    repo = re.sub(r"^https?://github\.com/", "", repo).strip("/")
    if repo.count("/") != 1:
        return redirect(f"/cases/{case_id}/sources", "Enter a repository as owner/repo.", "error")
    if acl not in {"team", "leads", "private"}:
        acl = "team"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Baton-Handover"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            meta = await client.get(f"https://api.github.com/repos/{repo}", headers=headers)
            if meta.status_code != 200:
                return redirect(
                    f"/cases/{case_id}/sources",
                    f"GitHub returned {meta.status_code}. Check the repository name or set GITHUB_TOKEN for private repos.",
                    "error",
                )
            issues_resp = await client.get(
                f"https://api.github.com/repos/{repo}/issues?state=all&per_page=20",
                headers=headers,
            )
            commits_resp = await client.get(
                f"https://api.github.com/repos/{repo}/commits?per_page=20",
                headers=headers,
            )
        issues = issues_resp.json() if issues_resp.status_code == 200 else []
        commits = commits_resp.json() if commits_resp.status_code == 200 else []
    except httpx.HTTPError as exc:
        return redirect(f"/cases/{case_id}/sources", f"GitHub import failed: {exc}", "error")

    issue_items = []
    for issue in issues[:20]:
        if "pull_request" in issue:
            continue
        issue_items.append(
            {
                "title": issue.get("title"),
                "owner": (issue.get("assignee") or {}).get("login"),
                "state": issue.get("state"),
                "date": (issue.get("created_at") or "")[:10],
                "due": issue.get("due_on"),
            }
        )
    commit_items = []
    for commit in commits[:20]:
        commit_items.append(
            {
                "sha": (commit.get("sha") or "")[:7],
                "date": ((commit.get("commit") or {}).get("author") or {}).get("date", "")[:10],
                "author": ((commit.get("commit") or {}).get("author") or {}).get("name"),
                "message": (commit.get("commit") or {}).get("message", "")[:300],
                "diff": "",
            }
        )
    payload = {"repository": repo, "issues": issue_items, "commits": commit_items}
    content = json.dumps(payload, indent=2)
    source_id = new_id("src")
    execute(
        """INSERT INTO sources
           (id, case_id, name, kind, acl, content, author, source_date, external_url, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            source_id,
            case_id,
            f"github_{repo.replace('/', '_')}.json",
            "repo",
            acl,
            content,
            "GitHub import",
            date.today().isoformat(),
            f"https://github.com/{repo}",
            now(),
        ),
    )
    audit(case_id, actor_for(case), "approver", "source_github", f"{repo} ({len(issue_items)} issues, {len(commit_items)} commits)")
    return redirect(
        f"/cases/{case_id}/sources",
        f"Imported {repo}: {len(issue_items)} issues and {len(commit_items)} commits. Run the pipeline to extract claims.",
    )


@app.post("/cases/{case_id}/sources/{source_id}/delete")
def delete_source(case_id: str, source_id: str):
    case = get_case(case_id)
    source = query_one("SELECT * FROM sources WHERE id = ? AND case_id = ?", (source_id, case_id))
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    execute("DELETE FROM sources WHERE id = ?", (source_id,))
    audit(case_id, actor_for(case), "approver", "source_deleted", source["name"])
    return redirect(f"/cases/{case_id}/sources", f"{source['name']} removed. Rebuild the ledger to reflect it.")


@app.post("/demo/reset")
def reset_demo():
    database.reset_all()
    seed_demo(force=True)
    build_case(CASE_ID)
    return redirect("/", "Demo data reset to its original state.")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "0.2.0",
        "cases": query_one("SELECT COUNT(*) AS n FROM cases")["n"],
        "claims": query_one("SELECT COUNT(*) AS n FROM claims")["n"],
    }


@app.get("/api/cases/{case_id}/state")
def api_state(case_id: str):
    case = get_case(case_id)
    claims = claims_with_evidence(case_id)
    conflicts = query("SELECT * FROM conflicts WHERE case_id = ? ORDER BY created_at", (case_id,))
    gaps = [c for c in claims if c["is_gap"]]
    sources = clickable_sources(case_id)
    return JSONResponse(
        {
            "case": case,
            "publish_rules": publish_rules(case_id),
            "counts": claim_counts(case_id),
            "sources": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "kind": s["kind"],
                    "acl": s["acl"],
                    "author": s["author"],
                    "source_date": s["source_date"],
                    "chunks": s["chunk_count"],
                    "redaction": {
                        "secrets": s["preview"]["secrets"],
                        "identities": s["preview"]["identities"],
                    },
                }
                for s in sources
            ],
            "claims": claims,
            "conflicts": conflicts,
            "gaps": gaps,
        }
    )
