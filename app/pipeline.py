from __future__ import annotations

import re
from collections import defaultdict

from app.db import audit, execute, get_db, new_id, now, query
from app.extract import extract_claims
from app.verify import (
    apply_verification,
    detect_conflicts,
    detect_gaps,
    find_chunk,
    most_restrictive,
    quote_in_text,
)


def chunk_text(text: str, size: int = 1400, overlap: int = 200) -> list[tuple[str, int, int]]:
    if not text:
        return []
    if len(text) <= size:
        return [(text, 0, len(text))]
    chunks: list[tuple[str, int, int]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            newline = text.rfind("\n", start + size // 2, end)
            if newline != -1:
                end = newline + 1
        chunks.append((text[start:end], start, end))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def clear_derived(case_id: str) -> None:
    with get_db() as db:
        db.execute(
            "DELETE FROM evidence WHERE claim_id IN (SELECT id FROM claims WHERE case_id = ?)",
            (case_id,),
        )
        db.execute("DELETE FROM conflicts WHERE case_id = ?", (case_id,))
        db.execute("DELETE FROM claims WHERE case_id = ?", (case_id,))
        db.execute("DELETE FROM chunks WHERE case_id = ?", (case_id,))


def build_case(case_id: str) -> dict:
    sources = query("SELECT * FROM sources WHERE case_id = ? ORDER BY created_at, id", (case_id,))
    if not sources:
        return {"ok": False, "error": "This case has no sources yet. Add a document or import a repository first."}

    clear_derived(case_id)
    sources_by_id = {s["id"]: s for s in sources}

    chunks_by_source: dict[str, list[dict]] = defaultdict(list)
    for source in sources:
        for idx, (text, start, end) in enumerate(chunk_text(source["content"])):
            chunk_id = f"ch_{source['id']}_{idx}"
            execute(
                """INSERT OR REPLACE INTO chunks
                   (id, case_id, source_id, idx, text, start_char, end_char)
                   VALUES (?,?,?,?,?,?,?)""",
                (chunk_id, case_id, source["id"], idx, text, start, end),
            )
            chunks_by_source[source["id"]].append(
                {
                    "id": chunk_id,
                    "source_id": source["id"],
                    "idx": idx,
                    "text": text,
                }
            )

    extracted = extract_claims(sources)
    inserted = 0
    for claim in extracted:
        source = sources_by_id.get(claim["source_id"])
        if not source:
            continue
        acl = most_restrictive([source["acl"]])
        if claim.get("acl"):
            acl = most_restrictive([acl, claim["acl"]])
        claim_id = new_id("clm")
        execute(
            """INSERT INTO claims
               (id, case_id, type, topic_key, text, value, owner, due_date, event_date,
                contact, basis, evidence_label, base_label, acl, status, is_gap,
                gap_critical, gap_accepted, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                claim_id,
                case_id,
                claim["type"],
                claim.get("topic_key"),
                claim["text"],
                claim.get("value"),
                claim.get("owner"),
                claim.get("due_date"),
                claim.get("event_date"),
                claim.get("contact"),
                claim.get("basis", "stated"),
                "PENDING",
                None,
                acl,
                "pending",
                0,
                0,
                0,
                now(),
            ),
        )
        for quote in claim.get("quotes", []):
            verified = quote_in_text(quote, source["content"])
            chunk = find_chunk(chunks_by_source.get(source["id"], []), quote)
            chunk_id = chunk["id"] if chunk else (
                chunks_by_source[source["id"]][0]["id"] if chunks_by_source.get(source["id"]) else None
            )
            execute(
                """INSERT INTO evidence
                   (id, claim_id, chunk_id, source_id, quote, verified, note)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    new_id("ev"),
                    claim_id,
                    chunk_id,
                    source["id"],
                    quote,
                    1 if verified else 0,
                    None,
                ),
            )
        inserted += 1

    apply_verification(case_id)
    conflicts = detect_conflicts(case_id)
    gaps = detect_gaps(case_id)

    counts = query(
        """SELECT evidence_label AS label, COUNT(*) AS n
           FROM claims WHERE case_id = ? GROUP BY evidence_label""",
        (case_id,),
    )
    audit(
        case_id,
        "System",
        "system",
        "pipeline_built",
        f"{len(sources)} sources, {inserted} claims, {len(conflicts)} conflicts, {len(gaps)} gaps",
    )
    return {
        "ok": True,
        "sources": len(sources),
        "claims": inserted,
        "conflicts": len(conflicts),
        "gaps": len(gaps),
        "counts": {row["label"]: row["n"] for row in counts},
    }
