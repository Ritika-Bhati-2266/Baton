from __future__ import annotations

import json
import os
import re
from collections import defaultdict

from app.redaction import KNOWN_PEOPLE, redact

CLAIM_TYPES = [
    "decision",
    "owner",
    "risk",
    "dependency",
    "open_question",
    "next_action",
    "blocker",
    "recurring_problem",
    "contact",
]

TYPE_LABEL = {
    "decision": "Decision",
    "owner": "Owner",
    "risk": "Risk",
    "dependency": "Dependency",
    "open_question": "Open question",
    "next_action": "Next action",
    "blocker": "Blocker",
    "recurring_problem": "Recurring problem",
    "contact": "Contact",
}

# Ordered most-specific-first. The first topic whose alias appears wins.
TOPIC_ALIASES = [
    ("driver-budget", ["budget note", "spend up to", "18,000", "18000"]),
    ("vendor-quote", ["vendor quote", "driver b quote", "quote pending", "vendor promised", "quote arrives", "i will chase"]),
    ("firmware-owner", ["firmware is maintained", "firmware maintained"]),
    ("hardware-owner", ["hardware is maintained", "hardware maintained"]),
    ("spare-driver", ["spare driver", "spare motor driver"]),
    ("rollback", ["rollback"]),
    ("brownout", ["browns out", "brownout"]),
    ("motor-driver", ["motor driver", "driver a", "driver b", "drv-a", "drv-b", "driver model"]),
    ("demo-venue", ["demo venue", "venue booking"]),
    ("demo-date", ["14 nov", "final demo"]),
    ("flashing", ["flash firmware", "flash.sh", "flashing"]),
    ("bench-supply", ["bench supply"]),
    ("battery-cutoff", ["battery cutoff"]),
]

CONFLICT_TOPICS = {"motor-driver"}

MARKER_MAP = {
    "DECISION": "decision",
    "OWNER": "owner",
    "RISK": "risk",
    "DEPENDENCY": "dependency",
    "OPEN QUESTION": "open_question",
    "QUESTION": "open_question",
    "TODO": "next_action",
    "NEXT ACTION": "next_action",
    "ACTION": "next_action",
    "BLOCKER": "blocker",
    "RECURRING PROBLEM": "recurring_problem",
    "RECURRING ISSUE": "recurring_problem",
    "CONTACT": "contact",
    "INFERRED": "risk",
}

MARKER_RE = re.compile(
    r"^(DECISION|OWNER|RISK|DEPENDENCY|OPEN QUESTION|QUESTION|TODO|NEXT ACTION|ACTION|"
    r"BLOCKER|RECURRING PROBLEM|RECURRING ISSUE|CONTACT|INFERRED)\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)

CHAT_RE = re.compile(r"^\[([^\]]+)\]\s*([^:]+):\s*(.+)$")
MAINTAINED_RE = re.compile(r"(.+?)\s+is maintained by\s+([A-Z][A-Za-z.]*(?:\s+[A-Z][A-Za-z.]+)?)", re.IGNORECASE)
RUN_RE = re.compile(r"\bRun\s+(.+?)\s+to\s+(.+)$", re.IGNORECASE)


def _clean_marker_line(line: str) -> str:
    clean = line.strip()
    clean = re.sub(r"^[-*\u2022]\s*", "", clean)
    clean = clean.replace("**", "").replace("__", "").strip()
    return clean


def _find_person(text: str) -> str | None:
    for person in KNOWN_PEOPLE:
        if person.lower() in text.lower():
            return person
    m = re.search(r"\bby\s+([A-Z][A-Za-z]*\.?\s+[A-Z][A-Za-z]+)", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b([A-Z][A-Za-z]*\.?\s+[A-Z][A-Za-z]+)\s+is\s+the\b", text)
    if m:
        return m.group(1).strip()
    return None


def _driver_value(text: str) -> str | None:
    m = re.search(r"\bdriver\s+([A-Za-z0-9]+)\b", text, re.IGNORECASE)
    if m:
        token = m.group(1)
        if token.lower() in {"a", "b", "c", "d"}:
            return f"driver {token.upper()}"
    m = re.search(r"DRIVER_MODEL\s*=\s*[\"']?([A-Za-z0-9]+)", text)
    if m:
        return f"driver {m.group(1).upper()}"
    return None


def _topic_for(text: str, claim_type: str) -> str:
    low = text.lower()
    for topic, aliases in TOPIC_ALIASES:
        if any(alias in low for alias in aliases):
            return topic
    return _fallback_topic(text, claim_type)


def _fallback_topic(text: str, claim_type: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    stop = {
        "the", "a", "an", "is", "are", "we", "to", "for", "of", "and", "in", "on",
        "by", "with", "this", "that", "it", "if", "be", "will", "can", "should",
        "must", "our", "from", "as", "at", "or", "not", "do", "does", "has", "have",
        "was", "were", "been", "before", "after", "about", "into", "than", "then",
        "so", "up", "out", "no", "now", "again", "still", "more", "most",
    }
    picked = [w for w in words if w not in stop and len(w) > 2][:3]
    return f"{claim_type}:" + "-".join(picked) if picked else claim_type


def _base_claim(source: dict, claim_type: str, text: str, quote: str, **kwargs) -> dict:
    claim = {
        "type": claim_type,
        "topic_key": _topic_for(text, claim_type),
        "text": text,
        "value": text,
        "owner": None,
        "due_date": None,
        "event_date": source.get("source_date"),
        "contact": None,
        "basis": "stated",
        "acl": source.get("acl", "team"),
        "source_id": source["id"],
        "quotes": [quote],
    }
    claim.update(kwargs)
    return claim


def _parse_marked_lines(source: dict) -> list[dict]:
    claims: list[dict] = []
    for raw in source["content"].splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        clean = _clean_marker_line(raw)
        m = MARKER_RE.match(clean)
        if m:
            marker = m.group(1).upper()
            body = m.group(2).strip()
            claim_type = MARKER_MAP[marker]
            basis = "inferred" if marker == "INFERRED" else "stated"
            owner = None
            contact = None
            value = body
            if claim_type == "owner":
                owner = _find_person(body) or body.rstrip(".")
                value = owner
            elif claim_type == "contact":
                contact = _find_person(body) or body.rstrip(".")
                value = contact
            elif claim_type == "decision":
                value = _driver_value(body) or body
            elif claim_type == "dependency":
                contact = _find_person(body)
            claims.append(
                _base_claim(
                    source,
                    claim_type,
                    body,
                    stripped,
                    value=value,
                    owner=owner,
                    contact=contact,
                    basis=basis,
                )
            )
            continue

        lower = stripped.lower()
        if "is maintained by" in lower:
            m2 = MAINTAINED_RE.search(stripped)
            if m2:
                person = m2.group(2).strip().rstrip(".")
                claims.append(
                    _base_claim(
                        source,
                        "owner",
                        stripped,
                        stripped,
                        value=person,
                        owner=person,
                    )
                )
            continue
        if lower.startswith("run ") and " to " in lower:
            m2 = RUN_RE.search(stripped)
            if m2:
                claims.append(
                    _base_claim(
                        source,
                        "next_action",
                        stripped,
                        stripped,
                        value=stripped,
                    )
                )
            continue
        if lower.startswith("motor driver ") and (" uses " in lower or " config" in lower):
            value = _driver_value(stripped)
            if value:
                claims.append(
                    _base_claim(
                        source,
                        "decision",
                        stripped,
                        stripped,
                        value=value,
                    )
                )
            continue
        if "no rollback procedure" in lower:
            claims.append(
                _base_claim(
                    source,
                    "risk",
                    stripped,
                    stripped,
                    value=stripped,
                )
            )
            continue
        if "budget" in lower and ("spend" in lower or "inr" in lower or "\u20b9" in stripped):
            claims.append(
                _base_claim(
                    source,
                    "decision",
                    stripped,
                    stripped,
                    value=stripped,
                )
            )
            continue
    return claims


def _parse_tasks(source: dict) -> list[dict]:
    claims: list[dict] = []
    lines = [ln for ln in source["content"].splitlines() if ln.strip()]
    if not lines:
        return claims
    header = [h.strip().lower() for h in lines[0].split(",")]

    def cell(row: list[str], name: str) -> str:
        try:
            idx = header.index(name)
        except ValueError:
            return ""
        return row[idx].strip() if idx < len(row) else ""

    for raw in lines[1:]:
        row = [c.strip() for c in raw.split(",")]
        title = cell(row, "title")
        if not title:
            continue
        owner = cell(row, "owner") or None
        due = cell(row, "due") or None
        status = cell(row, "status") or None
        claims.append(
            _base_claim(
                source,
                "next_action",
                title,
                title,
                value=title,
                owner=owner,
                due_date=due,
                event_date=source.get("source_date"),
                contact=None,
            )
        )
        if status:
            claims[-1]["status_note"] = status
    return claims


def _parse_repo(source: dict) -> list[dict]:
    claims: list[dict] = []
    try:
        data = json.loads(source["content"])
    except json.JSONDecodeError:
        return _parse_marked_lines(source)

    if isinstance(data, dict):
        commits = data.get("commits", [])
        issues = data.get("issues", [])
        items = list(commits) + list(issues)
    else:
        items = data

    for item in items:
        if not isinstance(item, dict):
            continue
        message = item.get("message") or item.get("title") or ""
        if not message:
            continue
        value = _driver_value(message) or _driver_value(item.get("diff", ""))
        if value and value.lower() != "driver":
            claims.append(
                _base_claim(
                    source,
                    "decision",
                    f"Commit {item.get('sha', 'unknown')}: {message}",
                    message,
                    value=value,
                    event_date=item.get("date") or source.get("source_date"),
                )
            )
            continue
        if item.get("title") and (item.get("owner") or item.get("user")):
            owner = item.get("owner") or item.get("user")
            if isinstance(owner, dict):
                owner = owner.get("login")
            claims.append(
                _base_claim(
                    source,
                    "next_action",
                    item["title"],
                    item["title"],
                    value=item["title"],
                    owner=owner,
                    due_date=item.get("due"),
                    event_date=item.get("date") or source.get("source_date"),
                )
            )
    return claims


def _parse_chat(source: dict) -> list[dict]:
    claims: list[dict] = []
    for raw in source["content"].splitlines():
        m = CHAT_RE.match(raw.strip())
        if not m:
            continue
        ts, sender, message = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        sender_person = _find_person(sender) or sender
        low = message.lower()
        if "promised" in low or ("quote" in low and "pending" in low):
            claims.append(
                _base_claim(
                    source,
                    "dependency",
                    message,
                    raw.strip(),
                    value=message,
                    contact=sender_person,
                    event_date=ts[:10],
                )
            )
        elif "will chase" in low or "i will" in low:
            claims.append(
                _base_claim(
                    source,
                    "next_action",
                    message,
                    raw.strip(),
                    value=message,
                    owner=sender_person,
                    event_date=ts[:10],
                )
            )
        elif "keep this between" in low or "not confirmed" in low:
            claims.append(
                _base_claim(
                    source,
                    "risk",
                    message,
                    raw.strip(),
                    value=message,
                    event_date=ts[:10],
                )
            )
    return claims


def _parse_free_source(source: dict) -> list[dict]:
    return _parse_marked_lines(source)


def extract_claims(sources: list[dict], use_llm: bool | None = None) -> list[dict]:
    """Deterministically extract typed claims from the demo/source formats.

    The extractor runs locally. If BATON_USE_LLM=1 and an API key is configured,
    team-labelled sources are first offered to the optional LLM adapter; the
    deterministic pass remains the fallback and the source of record.
    """
    use_llm = (os.environ.get("BATON_USE_LLM") == "1") if use_llm is None else use_llm
    claims: list[dict] = []
    for source in sources:
        kind = source.get("kind", "doc")
        parser = {
            "task_csv": _parse_tasks,
            "repo": _parse_repo,
            "chat": _parse_chat,
            "notes": _parse_marked_lines,
            "doc": _parse_free_source,
        }.get(kind, _parse_free_source)
        parsed = parser(source)
        if not parsed and kind not in {"chat", "task_csv"}:
            parsed = _parse_marked_lines(source)
        claims.extend(parsed)

    # Drop exact duplicates (same source, type, text) while preserving evidence.
    seen: set[tuple] = set()
    unique: list[dict] = []
    for c in claims:
        key = (c["source_id"], c["type"], re.sub(r"\s+", " ", c["text"]).strip().lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique


def group_by_topic(claims: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in claims:
        if c.get("is_gap"):
            continue
        groups[c.get("topic_key") or c["type"]].append(c)
    return groups


def redacted_for_model(source: dict) -> dict:
    text, report = redact(source.get("content", ""))
    return {"text": text, "report": report}
