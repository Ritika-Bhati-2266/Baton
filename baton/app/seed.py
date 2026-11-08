from __future__ import annotations

from app.db import execute, new_id, now, query_one

CASE_ID = "rover-demo"


DEMO_SOURCES = [
    {
        "id": "src_readme",
        "name": "README.md",
        "kind": "doc",
        "acl": "team",
        "author": "A. Rao",
        "source_date": "2026-10-02",
        "content": """# Autonomous Rover Club - Firmware README

Firmware is maintained by A. Rao.
Hardware is maintained by P. Kumar.

## Flashing the rover
Run ./scripts/flash.sh to flash firmware v2 to the rover before the 14 Nov demo.
The rover must be powered from the bench supply during flashing.

## Wiring notes
Motor driver A uses the pin map in config/motor_a.yaml.
The battery cutoff switch is on the left side of the chassis.

## Known limitation
There is no rollback procedure documented if a flash fails mid-way.
""",
    },
    {
        "id": "src_meeting_1012",
        "name": "meeting_notes_2026-10-12.md",
        "kind": "notes",
        "acl": "team",
        "author": "A. Rao",
        "source_date": "2026-10-12",
        "content": """# Meeting notes - 12 October 2026
Attendees: A. Rao, P. Kumar, S. Mehta

- DECISION: We are moving to motor driver B for the final demo.
- OWNER: Firmware is maintained by A. Rao.
- RISK: Driver B vendor quote is pending; if it arrives late we may miss the 14 Nov demo.
- OPEN QUESTION: Do we need a spare driver for the demo?
- DEPENDENCY: Demo venue booking depends on the college admin approval.
- TODO: Flash firmware v2 before the 14 Nov demo.
- NEXT ACTION: Contact S. Mehta about the vendor quote by 20 October.
- RECURRING PROBLEM: The rover browns out when both drive motors reverse at full speed.
- CONTACT: S. Mehta is the vendor contact for the motor driver order.
- INFERRED: The 14 Nov demo could slip if the vendor quote arrives late.
""",
    },
    {
        "id": "src_commits",
        "name": "commits.json",
        "kind": "repo",
        "acl": "team",
        "author": "A. Rao",
        "source_date": "2026-10-30",
        "content": """{
  "repository": "rover-club/rover-firmware",
  "commits": [
    {
      "sha": "a1b2c3d",
      "date": "2026-10-30",
      "author": "A. Rao",
      "message": "config: driver A pin map",
      "diff": "DRIVER_MODEL = \\"A\\"\\nPIN_MAP = config/motor_a.yaml"
    },
    {
      "sha": "e4f5g6h",
      "date": "2026-10-18",
      "author": "P. Kumar",
      "message": "add bench supply note to README",
      "diff": "+ The rover must be powered from the bench supply during flashing."
    }
  ]
}""",
    },
    {
        "id": "src_tasks",
        "name": "tasks.csv",
        "kind": "task_csv",
        "acl": "team",
        "author": "P. Kumar",
        "source_date": "2026-10-26",
        "content": """id,title,owner,status,due,source
42,Issue #42 - Flash firmware v2 to the rover before the 14 Nov demo,N. Singh,in progress,2026-11-14,GitHub Issue #42
43,Order a spare motor driver,,open,2026-11-01,Task board
44,Book the demo venue,P. Kumar,open,2026-11-07,Task board
""",
    },
    {
        "id": "src_chat",
        "name": "chat_export_oct.txt",
        "kind": "chat",
        "acl": "leads",
        "author": "S. Mehta",
        "source_date": "2026-10-25",
        "content": """[2026-10-20 10:02] S. Mehta: The vendor promised the driver B quote by 25 October.
[2026-10-25 16:40] S. Mehta: Quote still pending. I will chase the vendor again tomorrow.
[2026-10-25 16:42] A. Rao: Keep this between the leads for now, the price is not confirmed.
""",
    },
    {
        "id": "src_budget",
        "name": "budget_note_private.txt",
        "kind": "doc",
        "acl": "private",
        "author": "T. Lead",
        "source_date": "2026-10-24",
        "content": """Budget note (private): the club can spend up to 18,000 INR on driver B. Do not share outside the finance team.
""",
    },
]


def seed_demo(force: bool = False) -> None:
    existing = query_one("SELECT id FROM cases WHERE id = ?", (CASE_ID,))
    if existing and not force:
        return

    if existing:
        execute("DELETE FROM cases WHERE id = ?", (CASE_ID,))

    execute(
        """INSERT INTO cases
           (id, title, project, departing_name, incoming_name, approver_name,
            status, version, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            CASE_ID,
            "Rover Club handover: A. Rao to N. Singh",
            "Autonomous Rover Club",
            "A. Rao",
            "N. Singh",
            "T. Lead",
            "draft",
            1,
            now(),
        ),
    )

    for src in DEMO_SOURCES:
        execute(
            """INSERT INTO sources
               (id, case_id, name, kind, acl, content, author, source_date, external_url, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                src["id"],
                CASE_ID,
                src["name"],
                src["kind"],
                src["acl"],
                src["content"],
                src.get("author"),
                src.get("source_date"),
                src.get("external_url"),
                now(),
            ),
        )

    execute(
        "INSERT INTO audit(case_id, actor, role, action, detail, created_at) VALUES (?,?,?,?,?,?)",
        (CASE_ID, "System", "system", "demo_seeded", "Synthetic rover-club handover loaded", now()),
    )
