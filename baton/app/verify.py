from __future__ import annotations

import re
from collections import defaultdict

from app.db import execute, new_id, now, query, query_one
from app.extract import CONFLICT_TOPICS

PRIMARY_KINDS = {"repo", "task_csv", "doc", "github"}
INFORMAL_KINDS = {"notes", "chat"}

ACL_RANK = {"team": 1, "leads": 2, "private": 3}
ROLE_ACLS = {
    "incoming": {"team"},
    "departing": {"team", "leads"},
    "approver": {"team", "leads", "private"},
    "admin": {"team", "leads", "private"},
}

LABEL_ORDER = [
    "CONFLICTING",
    "QUOTE_FAILED",
    "MISSING",
    "INFERRED",
    "SINGLE_SOURCE",
    "HUMAN_ASSERTED",
    "VERIFIED",
    "PENDING",
]

LABEL_HELP = {
    "VERIFIED": "Quote checked and backed by a primary source or two independent informal sources.",
    "SINGLE_SOURCE": "Quote checked, but only one informal source supports it.",
    "CONFLICTING": "Two sources disagree on the same topic; both sides are shown.",
    "MISSING": "Expected knowledge has no evidence. A gap.",
    "HUMAN_ASSERTED": "Added or changed by a person; never shown as machine-verified.",
    "INFERRED": "A conclusion drawn from checked quotes; needs reviewer confirmation.",
    "QUOTE_FAILED": "Quote was not found in the cited source. Kept out of the reader view.",
    "PENDING": "Not verified yet.",
}


def normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def quote_in_text(quote: str, text: str) -> bool:
    q = normalize(quote)
    if not q:
        return False
    return q in normalize(text)


def most_restrictive(acls) -> str:
    best = "team"
    for acl in acls:
        if ACL_RANK.get(acl, 1) > ACL_RANK.get(best, 1):
            best = acl
    return best


def find_chunk(source_chunks: list[dict], quote: str) -> dict | None:
    for chunk in source_chunks:
        if quote_in_text(quote, chunk["text"]):
            return chunk
    return None


def compute_base_label(claim: dict, evidence_rows: list[dict], sources_by_id: dict[str, dict]) -> str:
    if claim.get("is_gap"):
        return "MISSING"
    if claim.get("basis") == "human":
        return "HUMAN_ASSERTED"
    if not evidence_rows:
        return "MISSING"
    if not all(row.get("verified") for row in evidence_rows):
        return "QUOTE_FAILED"
    kinds = {
        sources_by_id[row["source_id"]]["kind"]
        for row in evidence_rows
        if row.get("source_id") in sources_by_id
    }
    distinct_sources = {row["source_id"] for row in evidence_rows if row.get("source_id")}
    if claim.get("basis") == "inferred":
        return "INFERRED"
    if kinds & PRIMARY_KINDS:
        return "VERIFIED"
    if len(distinct_sources) >= 2 and kinds <= INFORMAL_KINDS:
        return "VERIFIED"
    if kinds <= INFORMAL_KINDS:
        return "SINGLE_SOURCE"
    return "VERIFIED"


def apply_verification(case_id: str) -> None:
    sources = query("SELECT * FROM sources WHERE case_id = ?", (case_id,))
    sources_by_id = {s["id"]: s for s in sources}
    chunks = query("SELECT * FROM chunks WHERE case_id = ?", (case_id,))
    chunks_by_source: dict[str, list[dict]] = defaultdict(list)
    for chunk in chunks:
        chunks_by_source[chunk["source_id"]].append(chunk)

    claims = query("SELECT * FROM claims WHERE case_id = ?", (case_id,))
    for claim in claims:
        evidence_rows = query("SELECT * FROM evidence WHERE claim_id = ?", (claim["id"],))
        verified_rows = []
        for row in evidence_rows:
            source = sources_by_id.get(row["source_id"])
            if not source:
                execute("UPDATE evidence SET verified = 0 WHERE id = ?", (row["id"],))
                row["verified"] = 0
                verified_rows.append(row)
                continue
            ok = quote_in_text(row["quote"], source["content"])
            execute("UPDATE evidence SET verified = ? WHERE id = ?", (1 if ok else 0, row["id"]))
            row["verified"] = 1 if ok else 0
            if ok and not row.get("chunk_id"):
                chunk = find_chunk(chunks_by_source.get(source["id"], []), row["quote"])
                if chunk:
                    execute("UPDATE evidence SET chunk_id = ? WHERE id = ?", (chunk["id"], row["id"]))
                    row["chunk_id"] = chunk["id"]
            verified_rows.append(row)
        label = compute_base_label(claim, verified_rows, sources_by_id)
        execute(
            "UPDATE claims SET base_label = ?, evidence_label = ? WHERE id = ?",
            (label, label, claim["id"]),
        )


def detect_conflicts(case_id: str) -> list[dict]:
    claims = query(
        "SELECT * FROM claims WHERE case_id = ? AND is_gap = 0 AND status != 'rejected'",
        (case_id,),
    )
    groups: dict[str, list[dict]] = defaultdict(list)
    for claim in claims:
        if claim["topic_key"] in CONFLICT_TOPICS and claim["type"] in {"decision", "owner"}:
            groups[claim["topic_key"]].append(claim)

    for topic, group in groups.items():
        by_value: dict[str, list[dict]] = defaultdict(list)
        for claim in group:
            by_value[normalize(claim["value"] or claim["text"])].append(claim)
        if len(by_value) < 2:
            continue
        value_groups = list(by_value.values())
        claim_a = value_groups[0][0]
        claim_b = value_groups[1][0]
        existing = query_one(
            "SELECT * FROM conflicts WHERE case_id = ? AND topic_key = ? AND status = 'open'",
            (case_id, topic),
        )
        if not existing:
            execute(
                """INSERT INTO conflicts
                   (id, case_id, topic_key, claim_a, claim_b, value_a, value_b,
                    status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("cfl"),
                    case_id,
                    topic,
                    claim_a["id"],
                    claim_b["id"],
                    claim_a["value"] or claim_a["text"],
                    claim_b["value"] or claim_b["text"],
                    "open",
                    now(),
                ),
            )
        for claim in group:
            execute(
                "UPDATE claims SET evidence_label = 'CONFLICTING' WHERE id = ?",
                (claim["id"],),
            )

    return query("SELECT * FROM conflicts WHERE case_id = ? ORDER BY created_at", (case_id,))


def _gap_evidence(source_claim: dict) -> dict | None:
    return query_one(
        "SELECT * FROM evidence WHERE claim_id = ? ORDER BY rowid LIMIT 1",
        (source_claim["id"],),
    )


def _insert_gap(
    case_id: str,
    source_claim: dict,
    text: str,
    critical: bool,
    kind_note: str = "",
) -> None:
    existing = query_one(
        """SELECT id FROM claims
           WHERE case_id = ? AND is_gap = 1 AND text = ? AND status != 'rejected'""",
        (case_id, text),
    )
    if existing:
        return
    gap_id = new_id("gap")
    execute(
        """INSERT INTO claims
           (id, case_id, type, topic_key, text, value, owner, due_date, event_date,
            contact, basis, evidence_label, base_label, acl, status, is_gap,
            gap_critical, gap_accepted, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            gap_id,
            case_id,
            source_claim["type"],
            source_claim["topic_key"],
            text,
            text,
            None,
            source_claim.get("due_date"),
            source_claim.get("event_date"),
            None,
            "stated",
            "MISSING",
            "MISSING",
            source_claim["acl"],
            "pending",
            1,
            1 if critical else 0,
            0,
            now(),
        ),
    )
    ev = _gap_evidence(source_claim)
    if ev:
        execute(
            """INSERT INTO evidence
               (id, claim_id, chunk_id, source_id, quote, verified, note)
               VALUES (?,?,?,?,?,?,?)""",
            (new_id("ev"), gap_id, ev["chunk_id"], ev["source_id"], ev["quote"], 1, kind_note),
        )


def detect_gaps(case_id: str) -> list[dict]:
    claims = query(
        "SELECT * FROM claims WHERE case_id = ? AND is_gap = 0 AND status != 'rejected'",
        (case_id,),
    )

    next_actions: dict[str, list[dict]] = defaultdict(list)
    for claim in claims:
        if claim["type"] == "next_action":
            next_actions[claim["topic_key"] or claim["id"]].append(claim)
    for group in next_actions.values():
        if any((c.get("owner") or "").strip() for c in group):
            continue
        claim = group[0]
        _insert_gap(case_id, claim, f"Task has no owner: {claim['text']}", True)

    for claim in claims:
        if claim["type"] == "dependency" and not (claim.get("contact") or "").strip():
            _insert_gap(case_id, claim, f"Dependency has no contact: {claim['text']}", True)

    for claim in claims:
        if "no rollback procedure" in normalize(claim["text"]):
            _insert_gap(case_id, claim, "No rollback procedure documented.", False)

    return query(
        "SELECT * FROM claims WHERE case_id = ? AND is_gap = 1 ORDER BY gap_critical DESC, created_at",
        (case_id,),
    )


def publish_rules(case_id: str) -> dict:
    open_conflicts = query(
        "SELECT * FROM conflicts WHERE case_id = ? AND status = 'open'", (case_id,)
    )
    critical_gaps = query(
        """SELECT * FROM claims
           WHERE case_id = ? AND is_gap = 1 AND gap_critical = 1 AND gap_accepted = 0
             AND status != 'rejected'""",
        (case_id,),
    )
    approved = query(
        "SELECT COUNT(*) AS n FROM claims WHERE case_id = ? AND status = 'approved'",
        (case_id,),
    )[0]["n"]
    pending = query(
        """SELECT COUNT(*) AS n FROM claims
           WHERE case_id = ? AND status = 'pending' AND is_gap = 0
             AND evidence_label NOT IN ('QUOTE_FAILED', 'CONFLICTING')""",
        (case_id,),
    )[0]["n"]
    reasons = []
    if open_conflicts:
        reasons.append(f"Resolve {len(open_conflicts)} conflicting claim(s) before publishing.")
    if critical_gaps:
        reasons.append(f"Answer or accept {len(critical_gaps)} critical gap(s) before publishing.")
    if approved == 0:
        reasons.append("Approve at least one claim before publishing.")
    return {
        "can_publish": not reasons,
        "reasons": reasons,
        "open_conflicts": open_conflicts,
        "critical_gaps": critical_gaps,
        "approved_count": approved,
        "pending_count": pending,
    }


def claims_with_evidence(case_id: str) -> list[dict]:
    claims = query(
        """SELECT c.*, s.name AS source_name, s.kind AS source_kind, s.source_date AS source_date
           FROM claims c
           LEFT JOIN evidence e ON e.claim_id = c.id
           LEFT JOIN sources s ON s.id = e.source_id
           WHERE c.case_id = ?
           ORDER BY c.is_gap DESC, c.created_at, c.id""",
        (case_id,),
    )
    seen = set()
    result = []
    for claim in claims:
        if claim["id"] in seen:
            continue
        seen.add(claim["id"])
        evidence = query(
            """SELECT e.*, s.name AS source_name, s.kind AS source_kind,
                      s.source_date AS source_date, s.acl AS source_acl
               FROM evidence e
               LEFT JOIN sources s ON s.id = e.source_id
               WHERE e.claim_id = ?
               ORDER BY e.rowid""",
            (claim["id"],),
        )
        claim["evidence"] = evidence
        result.append(claim)
    return result


def handover_claims(case_id: str, role: str) -> dict:
    allowed = ROLE_ACLS.get(role, ROLE_ACLS["incoming"])
    all_approved = query(
        """SELECT * FROM claims
           WHERE case_id = ? AND status = 'approved'
             AND evidence_label NOT IN ('QUOTE_FAILED')""",
        (case_id,),
    )
    visible = [c for c in all_approved if c["acl"] in allowed]
    hidden = [c for c in all_approved if c["acl"] not in allowed]
    for claim in visible:
        claim["evidence"] = query(
            """SELECT e.*, s.name AS source_name, s.kind AS source_kind,
                      s.source_date AS source_date, s.acl AS source_acl
               FROM evidence e
               LEFT JOIN sources s ON s.id = e.source_id
               WHERE e.claim_id = ?
               ORDER BY e.rowid""",
            (claim["id"],),
        )
    return {"visible": visible, "hidden_count": len(hidden)}
