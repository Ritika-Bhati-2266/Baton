"""End-to-end smoke test for the Baton Round 2 MVP.

Run from the baton/ directory:
    python scripts/smoke_test.py

It sets BATON_DATA_DIR to a temporary folder before importing the app, so the
demo database is isolated from local data.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="baton-smoke-")
os.environ["BATON_DATA_DIR"] = TMP

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def main() -> None:
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200, health.text
        assert health.json()["status"] == "ok"

        build = client.post("/cases/rover-demo/build", follow_redirects=False)
        if build.status_code != 303:
            fail(f"build returned {build.status_code}: {build.text[:400]}")

        state = client.get("/api/cases/rover-demo/state").json()
        labels = [c["evidence_label"] for c in state["claims"]]
        if "CONFLICTING" not in labels:
            fail("planted motor-driver conflict was not detected")
        if not any(c["is_gap"] and c["gap_critical"] for c in state["claims"]):
            fail("critical gaps were not detected")
        if not any(c["topic_key"] == "motor-driver" for c in state["claims"]):
            fail("motor-driver topic missing")

        # Publishing must be blocked before the human decisions.
        client.post("/cases/rover-demo/publish", follow_redirects=False)
        state = client.get("/api/cases/rover-demo/state").json()
        if state["case"]["status"] == "published":
            fail("publishing was allowed before conflicts and critical gaps were handled")

        # Resolve the conflict by keeping driver A.
        conflict = state["conflicts"][0]
        target = None
        for claim in state["claims"]:
            if claim["topic_key"] == conflict["topic_key"] and "driver a" in (claim["value"] or "").lower():
                target = claim
                break
        if not target:
            fail("could not find the driver A side of the conflict")
        resolved = client.post(
            f"/conflicts/{conflict['id']}/resolve",
            data={"claim_id": target["id"]},
            follow_redirects=False,
        )
        if resolved.status_code != 303:
            fail(f"conflict resolution returned {resolved.status_code}")

        # Accept/answer every gap so the publish gate can open.
        state = client.get("/api/cases/rover-demo/state").json()
        answered = False
        for gap in state["gaps"]:
            if gap["gap_accepted"]:
                continue
            if gap["gap_critical"] and not answered and "no owner" in gap["text"].lower():
                r = client.post(
                    f"/claims/{gap['id']}/answer-gap",
                    data={"answer": "N. Singh will own the spare driver order from this handover."},
                    follow_redirects=False,
                )
                answered = True
            else:
                r = client.post(f"/claims/{gap['id']}/accept-gap", follow_redirects=False)
            if r.status_code != 303:
                fail(f"gap action returned {r.status_code}")

        # Approve all remaining reviewable claims.
        approved = client.post("/cases/rover-demo/approve-all", follow_redirects=False)
        if approved.status_code != 303:
            fail(f"approve-all returned {approved.status_code}")

        published = client.post("/cases/rover-demo/publish", follow_redirects=False)
        if published.status_code != 303:
            fail(f"publish returned {published.status_code}")

        state = client.get("/api/cases/rover-demo/state").json()
        if state["case"]["status"] != "published":
            fail("case did not reach published state")
        if state["case"]["version"] != 1:
            fail(f"expected version 1, got {state['case']['version']}")
        if not state["case"]["published_at"]:
            fail("published_at was not recorded")

        # Incoming view must be gated, then filtered.
        incoming = client.get("/cases/rover-demo/handover?role=incoming")
        incoming_text = incoming.text
        if incoming.status_code != 200:
            fail(f"incoming handover returned {incoming.status_code}")
        forbidden = [
            "18,000",
            "keep this between the leads",
            "quote still pending",
        ]
        for needle in forbidden:
            if needle.lower() in incoming_text.lower():
                fail(f"restricted content leaked into the incoming view: {needle}")
        for expected in ["A. Rao", "N. Singh", "Flash firmware", "Start here"]:
            if expected.lower() not in incoming_text.lower():
                fail(f"expected handover content missing: {expected}")
        if "hidden for your role" not in incoming_text:
            fail("incoming view did not show the restricted-item placeholder count")

        # Approver preview can see the restricted claims.
        approver = client.get("/cases/rover-demo/handover?role=approver")
        for needle in ["18,000", "keep this between the leads"]:
            if needle.lower() not in approver.text.lower():
                fail(f"approver view missing restricted item: {needle}")

        # Acknowledgement closes the loop.
        ack = client.post("/cases/rover-demo/acknowledge", data={"role": "incoming"}, follow_redirects=False)
        if ack.status_code != 303:
            fail(f"acknowledge returned {ack.status_code}")
        state = client.get("/api/cases/rover-demo/state").json()
        if not state["case"]["acknowledged_at"]:
            fail("acknowledgement was not recorded")

        audit = client.get("/cases/rover-demo/audit")
        if "handover_published" not in audit.text or "handover_acknowledged" not in audit.text:
            fail("audit log is missing publish/acknowledge entries")

        print("SMOKE OK")
        print(f"  claims: {state['counts'].get('TOTAL')} (approved {state['counts'].get('approved')})")
        print(f"  conflicts: {len(state['conflicts'])}")
        print(f"  gaps: {len(state['gaps'])}")
        print(f"  labels: {sorted(set(labels))}")
        print(f"  temp data: {TMP}")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
