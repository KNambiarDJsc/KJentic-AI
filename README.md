# Kjentic-AI: ROA Agentic Orchestrator

An agentic case-handling system for hotel revenue-management (ROA) support cases. A case comes in, a **Planner Agent** understands it and picks worker agents, the agents gather evidence through tools, a **validation layer** checks every agent's result against a written contract, a **reporting layer** produces a report and a client draft, and **a human** decides whenever anything fails and approves the final response.

What makes it *agentic but governable*:

- **A harness, not scattered prompts.** Registries, flows, guardrails, schemas, prompts, fallback rules and validation policy live in versioned files under `harness/`. They are hash-locked: read-only on disk, verified when each case starts, and stamped on every report and trace.
- **One process, sequential agents.** The planner, all worker agents, validation and reporting run in a single hosted process. Worker agents run one at a time; there is no agent-to-agent communication.
- **Humans, not re-planners, handle failure.** Every failure pauses the case for a person: retry one agent, continue with a note, or abort. There is no autonomous re-plan.
- **Evidence, not assertions.** Agents cannot invent sources. The harness records every tool call and creates evidence from it; the validation layer checks each claim against those records.
- **Whole-orchestration observability.** OpenTelemetry, one trace per case (not per agent), shown in a separate dashboard with a waterfall, an agent-trajectory view and aggregate metrics, and embedded in the orchestrator UI.
- **Evals you can gate on.** A golden-case suite and a judge-calibration set run against the real models, with release gates.

> Status: a working proof of concept. Tools return deterministic placeholder data standing in for G3 and Salesforce. Nothing is ever sent to a client or written to Salesforce automatically.

---

## Contents

1. [How a case flows](#how-a-case-flows)
2. [Architecture](#architecture)
3. [Repository layout](#repository-layout)
4. [The harness](#the-harness)
5. [Planner Agent, guardrails and fallbacks](#planner-agent-guardrails-and-fallbacks)
6. [Worker agents and tools](#worker-agents-and-tools)
7. [Validation layer: how "complete" and "correct" are decided](#validation-layer)
8. [Reporting layer](#reporting-layer)
9. [Human in the loop](#human-in-the-loop)
10. [Observability, trajectory and the dashboard](#observability)
11. [Evaluation](#evaluation)
12. [Latency design](#latency-design)
13. [Setup and run](#setup-and-run)
14. [API reference](#api-reference)
15. [Configuration reference](#configuration-reference)
16. [Adding a new agent](#adding-a-new-agent)
17. [Design decisions and assumptions](#design-decisions-and-assumptions)
18. [Known limits and roadmap](#known-limits-and-roadmap)

---

## How a case flows

```
Case
 └─ intake: harness integrity check + input guardrails
     └─ Planner Agent
         ├─ Understanding Agent  (intent: topic hint, time horizon, property)
         └─ Planner              (picks a flow and agents; plan guardrails; fallbacks)
             └─ worker agents, ONE AT A TIME, in canonical order
                 └─ validation layer  (deterministic checks, then a semantic judge, per agent)
                     └─ reporting layer  (report.json, report.md, client draft)
                         └─ final human approval  (approve / request changes / reject)
                             └─ finalize
```

Whenever a step fails, the case pauses at a **human-in-the-loop** point instead of guessing:

| Stage | Trigger | Human options |
|---|---|---|
| `input` | Input guardrail failed (too short/long, prompt injection) | `continue`, `abort` |
| `plan` | No valid plan (planner unsure, guessed a topic the case does not support, or invalid) | `continue_with_agents` (choose agents), `abort` |
| `agent` | An agent failed, timed out, or hit a tool error | `retry`, `continue` (skip it), `abort` |
| `validation` | An agent's result failed validation | `retry:<agent_id>`, `continue_with_note`, `abort` |
| `report` | Always, before anything is final | `APPROVED`, `CHANGES_REQUESTED` (up to 2 times), `REJECTED` |

A retry re-runs **only that agent**; earlier agents are not re-run.

## Architecture

```
                  ┌──────────────────────── orchestrator process (:8100) ────────────────────────┐
  Browser  ─────► │ FastAPI  ─►  LangGraph skeleton (fixed)                                       │
  (UI + HIL)      │                intake → planner_agent → run_agent* → validate → report →      │
                  │                hil_final → finalize        (* one agent per step, sequential) │
                  │                                                                               │
                  │  reads (immutable)             writes (guarded)            emits              │
                  │  harness/  ◄── roa.harness ──► runtime/ via GuardedStore ── OpenTelemetry ──┐ │
                  │  registries, flows,            logs, results, memory,                       │ │
                  │  guardrails, prompts…          validation logs, reports                     │ │
                  └──────────────────────────────────────────────────────────────────────────────┼─┘
                                     LLM gateway (OpenAI-compatible)                             │ OTLP/HTTP
                                                                                                 ▼
                  ┌──────────────── dashboard process (:8200) ────────────────┐
                  │ OTLP receiver → sqlite spans → traces, waterfall,          │  ◄── also embedded in the
                  │ agent trajectory, aggregate metrics                        │      orchestrator UI (iframe)
                  └────────────────────────────────────────────────────────────┘
```

- The **LangGraph skeleton is fixed in code** (`roa/graph.py`). Everything it consults (which agents exist, which may run for which case type, what a guardrail allows, what counts as complete) is read from `harness/`.
- The graph carries only routing state. All business data lives in a sqlite case store (`roa/state.py`) that is written the instant something changes, so the UI shows live status independent of checkpoint timing.
- The **dashboard is a separate process**. The orchestrator only exports OTLP to it. Any OTLP backend (Jaeger, Tempo, Phoenix) could replace it.

## Repository layout

```
harness/          IMMUTABLE definitions (hash-locked)        runtime/   writable run data (git-ignored)
  manifest.json     models per role, LLM options, wiring       cases/<id>/…       per-case logs, results, reports
  agents/           agents.json + <id>/AGENT.md specs          agents/<id>/MEMORY.md   per-agent memory
  tools/            tools.json                                 audit.log.jsonl        refused write attempts
  flows/            flows.json (which agents per case type)
  guardrails/       guardrails.json                          roa/       the application
  planner/          PLANNER.md + planner.schema.json           graph.py           the LangGraph skeleton
  understanding/    UNDERSTANDING.md + schema                  planner/           Planner Agent (+ Understanding Agent)
  fallback/         fallback.json                              agents/            the four worker agents
  validation/       validation.json + JUDGE.md                 tools/             tool registry (placeholder data)
  reporting/        reporting.json + REPORT.md                 validation/        validation layer
                                                               reporting/         reporting layer
dashboard/        separate observability process               harness/           loader, write guard, guardrails
  server.py         OTLP receiver + APIs                       context.py         AgentContext (tools, logs, memory)
  trajectory.py     builds the agent trajectory                runner.py          runs an agent in-process
  static/index.html the UI                                     llm.py             gateway client
                                                               telemetry.py       OpenTelemetry helpers
web/index.html    orchestrator UI                              api.py             FastAPI service
evals/            golden cases, judge calibration, gates     tests/     pytest suite (offline, fake LLM)
scripts/          start.ps1 / stop.ps1                       docs/      ARCHITECTURE.md (one-page summary)
```

## The harness

Everything under `harness/` defines behaviour; nothing there is code.

| File | Purpose |
|---|---|
| `manifest.json` | Harness version, execution mode (sequential, no A2A, human-handled failures), **model per role**, LLM options (reasoning effort, token caps, timeout), pointers to every other component |
| `agents/agents.json` | Agent registry: id, entrypoint (`module:function`), version, enabled |
| `agents/<id>/AGENT.md` | Agent spec: JSON front matter (purpose, `handles` keywords, `tools_allowed`, `max_tool_calls`, timeout, **`expected_evidence`** contract) plus a markdown description |
| `tools/tools.json` | Tool registry: name, description, side effects. Cross-checked against `tools_allowed` and against the implementations at startup |
| `flows/flows.json` | Flow registry: per case type, the allowed agents, default agents and max agents; the canonical agent order; the human-option policy |
| `guardrails/guardrails.json` | Input, plan, agent, output and budget guardrails |
| `planner/`, `understanding/` | Prompts and JSON schemas for the Planner and the Understanding Agent (the schemas constrain the LLM output) |
| `fallback/fallback.json` | What to do when the LLM fails at each step (keyword match, minimal understanding, template draft, deterministic-only judging, human) |
| `validation/` | Which deterministic checks run and their severity; the semantic judge prompt |
| `reporting/` | Report sections and the client-draft prompt |

The loader **cross-validates** the harness at startup: every agent in a flow exists, every tool an agent may use is registered, every required tool is allowed, the planner schema's `case_type` enum equals the flow names, and every registered tool has an implementation. An inconsistent harness refuses to load.

### "Immutable but writable"

Definitions are immutable; logs and memory are writable. Enforced in code by `roa/harness/store.py`:

- `GuardedStore` refuses any write that resolves outside `runtime/`, so `harness/` cannot be written through it by any principal.
- A principal writes only inside its own subtree: `agent:<id>`, `planner`, `validator`, `reporter`, `orchestrator`.
- `*.jsonl` and `*.log` are **append-only**; other files are **write-once**, except an agent's `MEMORY.md` and the reporter's report files.
- Every refused attempt is appended to `runtime/audit.log.jsonl`.
- The harness directory is hashed at startup, set read-only on disk, and re-verified at every case intake. A mismatch stops the case.

This is an enforcement layer for trusted in-process code, **not a sandbox**: the principal is a string the caller passes. The read-only flag is best-effort, and the hash check *detects* tampering at the next case; it does not prevent it.

To change the harness on purpose: stop the servers, clear the read-only flag, edit, restart. The new hash is stamped on every subsequent case and report.

### What gets written where

```
runtime/cases/<case_id>/
  case.json                       the case as received
  orchestrator/log.jsonl          intake, plan, agent runs, validation, every human decision
  planner/log.jsonl               understanding output, plan proposed, fallbacks
  agents/<agent_id>/log.jsonl     the agent's own log
  agents/<agent_id>/tool_calls.jsonl   every tool call, recorded by the harness
  agents/<agent_id>/result.attemptN.json
  validation/<agent_id>.log.jsonl and .attemptN.json
  report/report.json, report.md, log.jsonl
runtime/agents/<agent_id>/MEMORY.md    the agent's persistent memory (only that agent can write it)
runtime/audit.log.jsonl               refused writes
```

## Planner Agent, guardrails and fallbacks

**Planner Agent = Understanding Agent + Planner** (`roa/planner/`).

1. The **Understanding Agent** (the intent agent) extracts a topic hint, time horizon, property and entities into a schema-constrained object.
2. The **Planner** sees the case, the understanding, the flow menu and the agent menu (built from the `AGENT.md` files) and returns `{case_type, agent_ids, reasoning}` matching `planner.schema.json`.
3. **Plan guardrails** check the plan: known flow, registered and enabled agents, no duplicates, non-empty, fits the flow's allowed agents, within the flow's max, and **topic-supported**: each agent must be backed by the case text (one of its `handles` keywords) or by the Understanding Agent's independent topic hint. A model that guesses a topic is blocked here.
4. **Fallback chain** (`fallback.json`): if the LLM fails or the plan is blocked, try deterministic keyword matching from the agent specs; if that finds nothing, pause for a human. It never guesses.

The planner prompt also has an explicit **abstain rule**: with no explicit topic in the case text, return an empty list.

Other guardrails: input length and prompt-injection patterns; per-agent tool-call limit and timeout; output guardrails on the client draft (every number must come from evidence, no forbidden promises or claimed actions, no internal terms or placeholders); a per-case LLM-call budget.

## Worker agents and tools

Four placeholder agents, each an `async def run(ctx, inp)` in `roa/agents/`:

| Agent | Purpose | Tools |
|---|---|---|
| `pricing_agent` | Rate, occupancy and competitor context; rate configuration | `read_g3_pricing`, `read_occupancy`, `read_competitor_rates`, `read_rate_configuration` |
| `overbooking_agent` | Booking status and inventory | `read_booking_status`, `read_inventory` |
| `lrv_agent` | Last Room Value and update history | `read_lrv`, `read_lrv_update_log` |
| `forecast_agent` | Occupancy forecast vs last year | `read_occupancy_forecast` |

Agents receive only an `AgentContext` (`roa/context.py`), the harness's gateway:

- `ctx.call_tool(name)` / `ctx.call_tools(a, b, c)`: enforces the agent's allow-list and call budget, opens a span, records the call (args, result, result hash, duration) to the agent's `tool_calls.jsonl`, and returns the record. `call_tools` runs **independent tools of one agent concurrently**; agents themselves stay sequential.
- `ctx.claim(text, value, call, confidence)`: creates evidence anchored to a recorded call. An agent cannot cite a source that was not really called.
- `ctx.log(...)`, `ctx.memory()`, `ctx.remember(...)`: the agent's own log and `MEMORY.md`.

The runner (`roa/runner.py`) loads the entrypoint, runs it under a span with a timeout, converts any exception into a `FAILED` task (which pauses for a human), and writes the result to the agent's own write-once file.

The Pricing Agent intentionally returns inconclusive, low-confidence evidence for `LONG_TERM` cases (it only reads rate configuration). That fails validation and goes to a human; it used to trigger an automatic re-plan.

## Validation layer

"What is complete" is **not in any prompt**. Each agent's contract is in its `AGENT.md` (`expected_evidence`: required tools, minimum evidence, confidence floors). The validation layer (`roa/validation/`) checks it per agent:

| Check | Fails when |
|---|---|
| `status_done` | The agent did not finish with `DONE` |
| `required_tools_called` | A tool the contract requires was never called |
| `min_evidence` | Fewer evidence items than the contract requires |
| `evidence_provenance` | An evidence item is not backed by a recorded tool call, or its values differ from the tool's output |
| `numbers_grounded` | A number in a claim does not appear in that tool's output |
| `confidence_floor` | Agent or evidence confidence is below the floor |
| `allowed_tools_only` | A tool outside the agent's allow-list was used |

Only if those pass does an **LLM judge** assess semantic relevance and sufficiency, against the same declared contract (it must not invent requirements, and a difference from the client's own figure is *not* a failure). If the judge is unavailable, the deterministic result stands and the verdict is flagged. Validators run **concurrently across agents** (they are independent) and each appends to its own log. A `FAIL` sends the case to a human.

## Reporting layer

`roa/reporting/` assembles, after validation, `report.json` and `report.md`: case, understanding, plan, guardrail events, per-agent evidence and verdicts, human decisions, caveats, LLM usage, harness version and hash, and the trace id.

The **client draft** is written from **verified evidence only** (agents that failed validation or were skipped are excluded and described to the client in topic-level language, never with agent names or check names). It passes output guardrails; if the LLM draft breaks one, or the LLM is down, a clean template draft is used instead. The reviewer can approve, request changes (bounded), or reject. Nothing is posted anywhere.

## Human in the loop

Decisions arrive through `POST /cases/{id}/hil` or the buttons in the UI. Each pending request lists the options valid for that stage; anything else is rejected with 422. LangGraph re-runs a node from the top on resume, so each interrupt sits in its own node, requests are idempotent, and side effects happen only after `interrupt()` returns. A resume can only be claimed once (a second attempt gets 409).

After a server restart, a case that was *waiting on a human* resumes normally from its checkpoint; a case that was *mid-run* is marked `FAILED` ("interrupted by a server restart"), and a resume that was claimed but not finished is released.

## Observability

OpenTelemetry with OpenInference-style span kinds. **One trace per case**: every stage, agent, tool call, LLM call (model, tokens, latency), guardrail, validator, reporter step and human wait. The harness runner opens every span; agents never touch a tracer.

A human wait can last hours, so no span is held open across one. The trace id and root span id are minted when the case is created and stored with it; each graph segment attaches to them as a remote parent; the wait is emitted as its own `hil.wait` span with explicit start and end times; and the root `case.orchestration` span is emitted once, when the case ends.

### Dashboard (`:8200`, separate process)

| Tab | Shows |
|---|---|
| **Traces** | Cases with stage, active time vs human-wait time, tokens, errors; a waterfall per case with a "collapse human waits" toggle and span attributes on click |
| **Trajectory** | The path each case took: Planner Agent (hint, horizon, plan and its source), each worker agent run with its **ordered tool calls checked against the agent's contract** (required and called, missing, extra), validation verdicts with failed checks, human decisions, retries as extra attempts. Metrics: agent runs, retries, tool correctness, missing/extra calls, first-try validation pass rate, human interventions and wait |
| **Metrics** | Latency by component (avg, p95, errors), validation verdicts and failed checks, human waits by stage, LLM usage by model |

### Connected to the orchestrator UI

Each case in the orchestrator UI has **Case / Trajectory / Trace** buttons that embed the live dashboard in place, a **↗** button to open it in a new tab, and a header **Dashboard** button for the cross-case metrics. The dashboard links back with "Open case in orchestrator". Deep links work both ways: `http://localhost:8100/ui/#case=<id>` and `http://localhost:8200/#trace=<id>&tab=trajectory`.

## Evaluation

```powershell
python -m evals.run                    # 12 golden cases + judge calibration (real models, a few minutes)
python -m evals.run --only judge       # just the judge calibration
python -m evals.run --cases pricing_long_horizon --repeats 3
python -m evals.run --otel             # also send the eval traces to the dashboard
```

- **Golden cases** (`evals/golden_cases.json`) run through the real graph in isolated temp directories, with a scripted human answering every pause. They cover routing for each case type, multi-agent routing, a typo-heavy case, a client figure that differs from the data, a long-horizon case that must fail validation and reach a human, and adversarial inputs (prompt injection, oversized input, an unroutable case). Each is scored on plan, human-pause path, validation outcome, final stage, grounding and draft safety.
- **Critical cases** (the adversarial and unroutable ones) must *all* pass: an average never hides a safety miss.
- **Judge calibration** (`evals/judge_cases.json`) scores the semantic judge against 10 expert-labeled evidence sets: false-pass rate (bad evidence let through) and false-block rate (good evidence blocked).
- **Release gates** are in `evals/thresholds.json`; the run exits non-zero if one is missed. Reports go to `evals/results/`.

Latest baseline (two runs per case, 24 runs): all passed, with identical plans on both repeats; grounding and draft safety 100%. The judge agreed with the labels on 9 of 10 examples: it never blocked good evidence but let through one off-topic set (LRV evidence on an overbooking case), a 20% false-pass rate, right at the gate. The suite is small: treat it as a baseline, not proof.

## Latency design

Pipeline overhead is negligible (a case that needs no LLM finishes in ~0.2 s). Time is LLM decoding, measured on the target gateway: `mistral-small` decodes at about 12 tokens/s, `gpt-oss-20b` at low reasoning effort about 4x faster, and at default effort it can spend 30–60 s "thinking". So:

- all four LLM roles use `gpt-oss-20b` at `reasoning_effort: low`, with per-role output-token caps;
- one keep-alive HTTP connection pool per event loop;
- independent work runs concurrently: validator judge calls across agents, and independent tool calls inside one agent. **Worker agents remain strictly sequential**, and a test enforces that.

Result: a single-agent case went from 20–34 s to 8–13 s end to end (average LLM time per case 19 s → 7.9 s). The speedup initially cost accuracy (the low-effort planner guessed an agent for vague cases), which is why the planner has the abstain rule and the topic-support guardrail, and why the evals treat those cases as critical.

## Setup and run

**Prerequisites:** Python 3.13+, and an OpenAI-compatible LLM gateway serving the models named in `harness/manifest.json` (default `gpt-oss-20b`). Any gateway works if you change the model names there.

```powershell
git clone https://github.com/KNambiarDJsc/Kjentic-AI.git
cd Kjentic-AI
python -m venv .venv ; .\.venv\Scripts\Activate.ps1      # recommended
pip install -r requirements.txt
copy .env.example .env                                     # then set ROA_LLM_BASE_URL and ROA_LLM_API_KEY
```

Start everything:

```powershell
scripts\start.ps1        # dashboard :8200 and orchestrator + UI :8100 (background)
scripts\stop.ps1
```

or run the two processes yourself (any OS):

```bash
python -m dashboard      # observability dashboard on :8200
python -m roa            # orchestrator + UI on :8100
```

Then open **http://localhost:8100/ui/** (orchestrator) and **http://localhost:8200/** (dashboard).

Run the tests (offline; a fake LLM, no gateway needed):

```bash
python -m pytest
```

## API reference

Orchestrator (`:8100`):

| Method and path | Purpose |
|---|---|
| `POST /cases` | Create a case `{description, property_name?, source?}`; returns `{case_id, trace_id}` and starts the run |
| `GET /cases`, `GET /cases/{id}` | Live case state (stage, plan, tasks, verdicts, guardrail events, pending human request) |
| `POST /cases/{id}/hil` | Answer the pending request `{hil_id, decision, comment?, agent_id?, agent_ids?}` |
| `GET /cases/{id}/report` | `report.md` and `report.json` |
| `GET /cases/{id}/files`, `GET /cases/{id}/files/{path}` | Everything the run wrote under `runtime/` for the case |
| `GET /agents` | Registered agents with contract and current `MEMORY.md` |
| `GET /harness` | Version, hash, integrity, flows, step policy |
| `GET /health`, `GET /config` | Health (with harness hash) and dashboard URL |

Dashboard (`:8200`): `POST /v1/traces` (OTLP/HTTP receiver), `GET /api/traces`, `GET /api/traces/{id}`, `GET /api/traces/{id}/trajectory`, `GET /api/metrics`, `GET /api/config`, `GET /api/health`.

## Configuration reference

Environment variables (a `.env` in the project root is read; prefix `ROA_`):

| Variable | Default | Meaning |
|---|---|---|
| `ROA_LLM_BASE_URL` | `http://localhost:4000/v1` | OpenAI-compatible gateway |
| `ROA_LLM_API_KEY` | (empty) | Gateway key. **Never commit it** |
| `ROA_HOST`, `ROA_PORT` | `0.0.0.0`, `8100` | Orchestrator bind |
| `ROA_OTLP_ENDPOINT` | `http://localhost:8200/v1/traces` | Where traces are exported |
| `ROA_DASHBOARD_URL` | `http://localhost:8200` | Dashboard link shown in the UI |
| `ROA_OTEL_ENABLED` | `true` | Turn trace export off |
| `ROA_HARNESS_DIR`, `ROA_RUNTIME_DIR`, `ROA_DATA_DIR` | `harness/`, `runtime/`, `data/` | Locations |
| `ROA_LOG_LEVEL` | `INFO` | Logging |
| `DASHBOARD_PORT`, `DASHBOARD_DB`, `ORCHESTRATOR_URL` | `8200`, `dashboard/data/spans.db`, `http://localhost:8100` | Dashboard process |

Model and latency settings are in `harness/manifest.json`: `models` (per role), `llm.reasoning_effort` and `llm.max_tokens` (per role), `llm.timeout_s`.

## Adding a new agent

1. **Tools:** implement each in `roa/tools/__init__.py` with `@tool("name")` and register it in `harness/tools/tools.json`.
2. **Agent:** write `roa/agents/<name>.py` with `async def run(ctx, inp) -> AgentTaskResult`, using `ctx.call_tool(s)` and `ctx.claim(...)`.
3. **Spec:** add `harness/agents/<id>/AGENT.md` (purpose, `handles`, `tools_allowed`, `expected_evidence`) and an entry in `harness/agents/agents.json`.
4. **Flows:** add the agent to the relevant flows' `allowed_agents` (and `canonical_agent_order`) in `harness/flows/flows.json`.
5. **Test it:** the loader cross-checks steps 1–4 at startup. Add unit tests and golden cases in `evals/golden_cases.json`.

No graph or orchestration code changes are needed.

## Design decisions and assumptions

- **Sequential agents, no A2A.** Worker agents run one after another in a single process; they never call each other.
- **Async agents.** Agents are `async` functions, so I/O-bound tool work inside an agent can overlap, but the graph advances exactly one agent per step.
- **Human in the loop for failure, not a re-planner.** Recovery decisions belong to a person.
- **Fixed skeleton, configurable behaviour.** The graph is code; agents, flows, guardrails, prompts and policy are files.
- **The quality plane does not trust the execution plane.** Evidence comes from harness-recorded tool calls; the judge sees only the declared contract and returned evidence.
- **Deterministic before LLM.** Cheap checks decide first; the LLM judge covers only what code cannot.

## Known limits and roadmap

- Tools are deterministic placeholders. Real G3 and Salesforce tools plug in through the tool registry.
- No API authentication yet: anyone who can reach port 8100 can approve cases. Add auth before exposing it beyond a trusted network.
- Single process and sqlite: fine for a POC; only light concurrency has been exercised.
- A restart marks running cases as interrupted instead of resuming them mid-run.
- The judge has a known false-pass on off-topic evidence attached to a case about a different topic; tightening its prompt is the next fix.
- No historical-case retrieval or learning yet. A retrieval tool plus agent `MEMORY.md` is the intended place.
- No alerting; the dashboard shows failures but does not notify.
- The eval suite is small (12 golden cases, 10 judge examples); grow it from real cases.
