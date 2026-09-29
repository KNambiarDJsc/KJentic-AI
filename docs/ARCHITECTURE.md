# ROA Agentic Orchestrator: Architecture Summary

**What it is.** A case-handling orchestrator for ROA support cases. One process hosts a Planner Agent, four worker agents, a validation layer and a reporting layer. Every agent action is traced, every result is checked against a written contract, and a human decides whenever something fails. Nothing is sent to a client or written to Salesforce automatically.

## How a case flows

```
Case -> intake guardrails -> Planner Agent (Understanding Agent, then Planner)
     -> worker agents, one at a time -> validation layer (per agent)
     -> reporting layer (report + client draft) -> human approval -> done
```

Any failure (blocked input, no valid plan, agent failure, failed validation) pauses the case for a person, who can retry one agent, continue with a note, or abort. There is no autonomous re-planning and agents never talk to each other.

## The harness (what makes it agentic and governable)

All behaviour is defined in files under `harness/`, not in prompts scattered through code:

| Component | File(s) |
|---|---|
| Agent registry and per-agent specs (purpose, tools, expected evidence) | `agents/agents.json`, `agents/<id>/AGENT.md` |
| Tool registry | `tools/tools.json` |
| Flow registry (which agents may run for which case type) | `flows/flows.json` |
| Guardrails (input, plan, agent, output, budgets) | `guardrails/guardrails.json` |
| Planner and understanding schemas and prompts | `planner/`, `understanding/` |
| Fallback rules | `fallback/fallback.json` |
| Validation and reporting policy | `validation/`, `reporting/` |

The harness is hash-locked: it is read-only on disk, its hash is verified when each case starts, and the hash is stamped on every report and trace. Agents write only their own logs and `MEMORY.md` under `runtime/`, through one guarded write path that refuses anything else and audits every refusal.

## How "complete" and "correct" are decided

Not by an LLM guessing. Each agent's contract (required tools, minimum evidence, confidence floors) is in its `AGENT.md`. The validation layer checks it deterministically: required tools were called, every piece of evidence traces to a tool call the harness recorded, every number in a claim appears in that tool's output, confidence is above the floor. Only then does an LLM judge assess semantic relevance, against the same declared contract. Agents cannot invent evidence: the harness creates it from tool calls.

## Observability

OpenTelemetry, one trace per case, covering every stage, agent, tool call, LLM call, guardrail, validator, report step and human wait. A separate dashboard process (port 8200) receives the traces and shows a waterfall per case plus aggregate metrics: latency by component, validation verdicts and failed checks, human-wait time by stage, and tokens by model. Human waits can last hours, so they are recorded as their own spans and never held open. Nothing about tracing lives inside the agents.

### Agent trajectory

The dashboard's **Trajectory** view shows the path each case actually took: the Planner Agent (topic hint, horizon, plan and its source), then each worker agent run with its ordered tool calls checked against the agent's declared contract (required tools called, missing, extra), each validation verdict with the failed checks, and every human decision. Retries appear as extra attempts. Summary metrics: agent runs and retries, tool correctness, missing and extra tool calls, first-try validation pass rate, human interventions and wait time. It is rebuilt from the same OpenTelemetry spans, so it needs nothing beyond the trace.

The orchestrator UI and the dashboard are connected both ways: each case in the orchestrator UI has Case / Trajectory / Trace buttons that embed the live dashboard in place (plus a new-tab link and an overall Dashboard button), and the dashboard links back with "Open case in orchestrator". Deep links work in both directions (`/ui/#case=<id>`, `:8200/#trace=<id>&tab=trajectory`).

## Evaluation

`python -m evals.run` runs 12 golden cases through the real graph and models: routing, human-pause behaviour, validation outcomes, grounding, draft safety, and adversarial inputs (prompt injection, oversized input, unroutable cases). It also calibrates the LLM judge against 10 labeled evidence sets (false-pass and false-block rates). Release gates are in `evals/thresholds.json`; the run exits non-zero if any is missed.

Latest run (2026-09-29, two runs per case, 24 runs): all passed, with identical plans on both repeats. Routing, human-pause path, validation outcome, grounding and draft safety are all 100%, at about 3.2 LLM calls and 1,900 tokens per case. The judge agreed with the labels on 9 of 10 examples: no good evidence was blocked, but it let through one off-topic set (LRV evidence attached to an overbooking case), a 20% false-pass rate, right at the gate. The suite is small, so treat these as a baseline, not proof.

## Latency

Pipeline overhead is negligible (cases that need no LLM finish in 0.2 s); time is LLM decoding on the GX10 gateway. Measured: `mistral-small` decodes at about 12 tokens/s, while `gpt-oss-20b` at low reasoning effort runs about 4x faster (at default effort it can spend 30-60 s "thinking"). Changes, all in the harness manifest and code:

- All four LLM roles use `gpt-oss-20b` at `reasoning_effort: low`, with a per-role output-token cap so no call can run away.
- One keep-alive HTTP connection pool per event loop.
- Independent work runs concurrently: validator judge calls across agents, and independent tool calls inside one agent. Worker agents stay strictly sequential (a test guards this).

Result: a single-agent case went from 20-34 s to 8-13 s end to end; average LLM time per case from 19 s to 7.9 s. The speedup initially cost accuracy (the low-effort planner guessed an agent for a vague case, 50% of the time in a benchmark), so the planner prompt got an explicit abstain rule and a deterministic guardrail now blocks any plan not backed by the case text or the understanding agent's independent topic hint. Safety cases (prompt injection, oversized input, unroutable case) are marked critical in the evals: an average can never hide a miss.

## Deliberate limits (honest list)

- Tools are placeholders with deterministic data. Real G3 and Salesforce tools plug in through the tool registry.
- Immutability is enforcement for in-process code, not a sandbox; tampering is detected at the next case, not prevented.
- No API authentication yet. Single process, sqlite: fine for a POC, not for load.
- No historical-case retrieval yet. The agent `MEMORY.md` files and a retrieval tool are the intended place for it.
- A server restart marks running cases as interrupted; cases waiting on a human resume normally.
