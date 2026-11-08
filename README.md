# Baton

**An evidence-linked, permission-aware knowledge handover platform.**

Baton is the ByteStorm submission for the CodeFrontier Hackathon, Round 2. It turns scattered, authorised project activity into a reviewed continuation plan for one departing member and one incoming member.

- **Live demo:** <https://baton-t3ax.onrender.com>
- **Source code:** <https://github.com/Ritika-Bhati-2266/Baton>
- **Round 2 report:** [docs/Baton_Round2_Report.pdf](docs/Baton_Round2_Report.pdf)

The central idea is simple: **every machine-generated statement in a handover is a claim with a checked quote, a visible evidence label and an access label.** Human-added content is labelled as such, and nothing reaches the incoming person until a human approves it.

---

## Quick start (Windows)

```powershell
cd Baton
.\run.ps1
```

Then open <http://127.0.0.1:8000>.

## Quick start (macOS / Linux / Docker)

```sh
cd Baton
sh run.sh
# or
docker build -t baton .
docker run --rm -p 8000:8000 baton
```

No API key is required. The demo corpus and the deterministic verifier run fully offline. The demo case is seeded automatically on first start.

---

## The judge's 90-second path

1. Open the dashboard at `/`.
2. Click **Open the demo review console**.
3. The review console shows:
   - a planted **CONFLICTING** decision: meeting notes say motor driver B, while the README and the latest commit say driver A;
   - **critical MISSING gaps**: an unowned spare-driver task and a dependency with no contact;
   - a leads-only chat source and a private budget note that are clearly labelled.
4. Click **Keep this side** on the driver-A side of the conflict.
5. Answer or accept the critical gaps.
6. Click **Approve all reviewable**, then **Publish handover**.
7. The incoming person's view opens. Notice:
   - restricted claims are **counted, not rendered**;
   - every claim has a quote and a source;
   - accepted gaps are shown as known gaps;
   - the first-week plan and ownership are explicit.
8. Switch the role tabs to compare the **incoming**, **departing** and **approver** views.
9. Open the **Audit** page to see the append-only record of approvals, edits, resolutions, publishing and acknowledgement.

The demo data is synthetic. Reset it at any time from the dashboard.

---

## What is implemented (Round 2)

### Must features

| Feature | Status | Where |
|---|---|---|
| Document/PDF/Markdown upload connector | Done | `app/main.py`, `sources.html` |
| GitHub read-only import (public issues + commits) | Done | `POST /cases/{id}/sources/github` |
| Typed claim extraction with quote, source, date, author | Done | `app/extract.py`, `app/pipeline.py` |
| Deterministic quote verifier | Done | `app/verify.py` |
| Evidence labels (Verified, Single-source, Conflicting, Missing, Human-asserted, Inferred, Quote-failed) | Done | `app/verify.py`, `review.html` |
| Conflict detection by topic key | Done | `detect_conflicts` |
| Gap rules and critical gap gate | Done | `detect_gaps` |
| Review console: edit, reject, approve per claim | Done | `app/main.py`, `review.html` |
| Conflict resolution (keep a side / reject both) | Done | `POST /conflicts/{id}/resolve` |
| Role-based views (approver / departing / incoming) | Done | `handover_claims`, `handover.html` |
| Source-level access labels and claim inheritance | Done | `app/verify.py`, `app/pipeline.py` |
| Secret stripping and identity pseudonymisation preview | Done | `app/redaction.py`, `sources.html` |
| Append-only audit log | Done | `audit` table, `audit.html` |
| Publish gate: unresolved conflicts and critical gaps block sharing | Done | `publish_rules` |

### Additional Round 2 features

- Acknowledgement of receipt (a lightweight Round 3 preview).
- Human answer to a gap, stored as `HUMAN_ASSERTED`.
- Inferred claims that require explicit reviewer confirmation.
- JSON state API at `/api/cases/{id}/state` and automatic OpenAPI docs at `/docs`.
- Docker + Render deployment configuration.
- End-to-end smoke test (`python scripts/smoke_test.py`).

---

## How the trust model works

### 1. Ingestion

Each source is stored with:

- a stable source ID,
- a kind (`doc`, `notes`, `task_csv`, `repo`, `chat`),
- an access label (`team`, `leads`, `private`),
- an author and source date,
- the original text.

Text is chunked into stable IDs with character offsets. A redaction preview shows exactly what an external model would see: secrets and known identities are replaced before any outbound call.

### 2. Extraction

The default extractor is deterministic and local. It recognises:

- explicit markers: `DECISION:`, `OWNER:`, `RISK:`, `DEPENDENCY:`, `OPEN QUESTION:`, `TODO:`, `NEXT ACTION:`, `BLOCKER:`, `RECURRING PROBLEM:`, `CONTACT:`, `INFERRED:`;
- README ownership sentences (`X is maintained by Y`);
- run instructions (`Run ... to ...`);
- motor-driver configuration statements;
- GitHub-style commit/issue JSON;
- task CSV rows;
- chat-export lines.

Every claim keeps the **exact quote** that supports it. The model is optional; the verifier is mandatory.

### 3. Verification

A deterministic verifier normalises whitespace and checks that the quote is a substring of the cited source. It never asks a model to grade its own output.

Label rules:

| Label | Rule |
|---|---|
| `VERIFIED` | Quote exists and is backed by a primary source (repo, tracked ticket, formal document) or two independent informal sources. |
| `SINGLE_SOURCE` | Quote exists, but only one informal source (notes or chat) supports it. |
| `CONFLICTING` | Same topic key, different values. Both sides stay visible. |
| `MISSING` | A gap rule found expected knowledge without evidence. |
| `HUMAN_ASSERTED` | Added or edited by a person. Never shown as machine-verified. |
| `INFERRED` | A conclusion drawn from checked quotes; requires reviewer confirmation. |
| `QUOTE_FAILED` | The quote was not found. It stays out of the reader view. |

### 4. Conflict and gap rules

- Conflicts are detected only for value topics (the demo uses `motor-driver`) and only among decision/owner claims.
- Critical gaps:
  - a next-action topic with no owner anywhere in the ledger;
  - a dependency with no contact.
- Non-critical gap:
  - the source explicitly says no rollback procedure exists.
- Publishing is blocked until every open conflict is resolved and every critical gap is answered or explicitly accepted.

### 5. Permissions

- Claims inherit the most restrictive access label of their evidence.
- Role filtering happens **in code**, not in a prompt.
- The incoming person sees `team` claims only.
- Restricted claims render as a count and a placeholder, never as partial text.
- The automated smoke test asserts that leads-only and private text cannot appear in the incoming view.

---

## Demo scenario (synthetic)

The rover-club dataset contains:

| Source | Access | What it contributes |
|---|---|---|
| `README.md` | team | ownership, flashing instructions, driver A wiring, no rollback note |
| `meeting_notes_2026-10-12.md` | team | driver B decision, risks, dependency, open question, contacts |
| `commits.json` | team | driver A commit that conflicts with the meeting decision |
| `tasks.csv` | team | owned and unowned tasks, including a missing owner |
| `chat_export_oct.txt` | **leads only** | vendor quote dependency and confidential price context |
| `budget_note_private.txt` | **private** | driver-B budget that must never reach the incoming person |

Planted failures:

1. Driver B (meeting) vs driver A (README + commit) â€” **conflict**.
2. Spare-driver task with no owner â€” **critical gap**.
3. Demo venue dependency with no contact â€” **critical gap**.
4. No rollback procedure â€” **non-critical gap**.
5. Leads-only vendor messages and a private budget note â€” **permission test**.

---

## Architecture

```
Browser
  â”‚  HTML + small JS (server-rendered Jinja templates)
  â–¼
FastAPI (app/main.py)
  â”‚
  â”œâ”€â”€ ingestion (upload / GitHub / demo seed)
  â”œâ”€â”€ chunking + redaction preview        app/pipeline.py, app/redaction.py
  â”œâ”€â”€ deterministic extraction            app/extract.py
  â”œâ”€â”€ deterministic verifier              app/verify.py
  â”œâ”€â”€ claim ledger + conflicts + gaps     SQLite (app/db.py)
  â”œâ”€â”€ review actions + publish gate       app/main.py, app/verify.py
  â””â”€â”€ role-filtered handover views         app/verify.py, handover.html
```

### Technology

- **Backend:** FastAPI + Python 3.12
- **Templates:** Jinja2
- **Storage:** SQLite (single file, WAL mode)
- **Parsing:** `pypdf` for PDFs
- **GitHub:** `httpx` against the REST API
- **Deployment:** Docker, Render (`render.yaml`)
- **Tests:** `pytest`-compatible smoke script using FastAPI `TestClient`

### Why a single FastAPI service instead of Next.js + FastAPI?

The Round 1 plan proposed a split Next.js/FastAPI stack. Within the Round 2 build window, the priority is a **reliable, judge-usable end-to-end workflow**. A single deployable service removes CORS, two build systems and a second deployment target while preserving the parts that matter to the trust model: the claim schema, the deterministic verifier, permission filtering and the review gate. The frontend remains server-rendered and fully interactive through forms. A React frontend can be added in Round 3 without changing the ledger or API.

---

## Data model

- `cases` â€” handover case, status, published version, acknowledgement.
- `sources` â€” authorised inputs with ACL and metadata.
- `chunks` â€” stable source windows with character offsets.
- `claims` â€” typed claims with value, owner, due date, event date, contact, basis, evidence label and status.
- `evidence` â€” quote + chunk + source + verification result.
- `conflicts` â€” topic-level disagreement with resolution state.
- `audit` â€” append-only action log.

The JSON state API exposes all of this at `/api/cases/{id}/state`.

---

## Optional integrations

### GitHub

- Use the import form on the Sources page, or:
  ```http
  POST /cases/{id}/sources/github
  repo=owner/repository
  acl=team
  ```
- Public repositories work without credentials.
- Set `GITHUB_TOKEN` in the environment for private repositories. The token is used only for the request and is never stored.

### Optional LLM adapter

The deterministic extractor is the source of record. `BATON_USE_LLM=1` plus an Anthropic key is reserved for a future adapter. The pipeline only considers `team`-labelled sources for any outbound call; `leads` and `private` sources stay local. The deterministic verifier still checks every returned quote, so an LLM cannot promote its own claim to `VERIFIED`.

---

## Testing

```powershell
cd Baton
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\smoke_test.py
```

The smoke test runs the full journey and asserts:

- the planted conflict and critical gaps are detected;
- publishing is blocked before human decisions;
- conflict resolution and gap handling update the gate;
- publishing produces v1;
- the incoming view does not contain leads-only or private text;
- the approver view does contain those claims;
- acknowledgement and audit records are written.

---

## Deployment

### Render

1. Push the repository to GitHub.
2. Create a new Render Blueprint and point it at `render.yaml`, or create a Docker web service with the included `Dockerfile`.
3. Set `BATON_DATA_DIR=/tmp/baton` (already in `render.yaml`).
4. Deploy and open the service URL. The demo seeds itself.

### Docker

```sh
docker build -t baton .
docker run --rm -p 8000:8000 -e BATON_DATA_DIR=/tmp/baton baton
```

SQLite lives inside the container. For a persistent deployment, mount a volume and point `BATON_DATA_DIR` at it. If the filesystem is ephemeral, the demo reseeds on restart.

---

## Changes from the Round 1 plan

| Round 1 plan | Round 2 implementation | Why |
|---|---|---|
| Next.js + FastAPI + PostgreSQL | FastAPI + Jinja + SQLite | One deployable service; reliable end-to-end demo within the window. Schema and verifier unchanged. |
| Claude extraction as the primary path | Deterministic local extraction; LLM adapter optional | The demo must work without a paid key, and the trust story is stronger when the extractor is auditable. |
| Live task/chat connectors in Round 3 | Chat export and task CSV connectors already work | They were cheap to add and strengthen the permission demo. |
| Redis/RQ background jobs | Synchronous pipeline with progress feedback | The demo corpus is small; a job queue is a scale concern, not a correctness concern. |
| Row-level security | Application-level claim filtering + automated role test | The user-visible guarantee is tested: zero restricted claims in the incoming view. |
| PDF export, version diff, question flow, acknowledgement in Round 3 | Acknowledgement and gap answers are partially implemented; version diff and PDF export remain Round 3 | They are meaningful, visible additions for the next round. |

---

## Three committed improvements for Round 3

1. **Departing-member question flow with notification and structured answers.** Gaps become targeted questions routed to the departing member; answers are stored as `HUMAN_ASSERTED` evidence and re-verified where possible.
2. **Version diff and decision timeline across publishes.** Show what changed between handover versions (claims added, removed, re-labelled, re-owned), with a reader-facing "what changed since vN" view.
3. **Incoming-person feedback loop and section-level acknowledgement.** The incoming person can confirm, dispute or request access per section; disputes re-open the claim for the approver, and the audit log captures the full loop.

---

## Known limitations

- Authentication is simulated with role tabs so judges can open every link without signing in. In production, roles would come from an authenticated session or SSO.
- SQLite is single-node. A multi-user deployment should move the ledger to PostgreSQL.
- The deterministic extractor is strongest on structured notes and exports. Free prose needs the optional LLM adapter or careful marker use.
- The GitHub connector is read-only and capped at 20 issues/commits per import.
- PDF text extraction depends on the PDF having a text layer.
- The redaction preview is a demonstration; production pseudonymisation must be tested against the exact verifier path.
- No email/notification delivery in this round; the departing-member question flow is a Round 3 improvement.

---

## Responsible design

- All demo data is synthetic. No confidential workplace material is used.
- Connecting a source does not make it shareable; the access label is explicit at upload time.
- Restricted claims are filtered server-side and rendered only as a count.
- Human edits and gap answers are never presented as machine-verified.
- The audit log records who approved, edited, resolved, published and acknowledged.
- The product does not monitor employees; it operates on one project at the moment a handover is needed.

---

## Team

**ByteStorm** â€” solo submission.

- Ritika Bhati â€” team lead, backend, verification, frontend, product and QA.

AI assistance was used to structure, draft and revise the plan and the implementation. The verifier, permission tests and demo behaviour are executable and can be inspected in the repository.

