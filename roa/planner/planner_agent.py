"""Planner Agent = Understanding Agent (intent) + Planner.

Both sub-steps are driven by files in harness/ (prompts, JSON schemas, agent registry, flow
registry, guardrails, fallback rules). Failure never triggers autonomous re-planning: the
fallback chain is LLM -> deterministic keyword match -> human in the loop.
"""

import json
from dataclasses import dataclass, field

from roa import llm, state, telemetry
from roa.harness import guardrails
from roa.harness.loader import HarnessBundle
from roa.harness.store import GuardedStore
from roa.models import CaseUnderstanding, GuardrailEvent, Plan, TimeHorizon

PRINCIPAL = "planner"


@dataclass
class PlanOutcome:
    plan: Plan | None
    events: list[GuardrailEvent] = field(default_factory=list)
    reason: str = ""


def _log(store: GuardedStore, case_id: str, event: str, **fields):
    store.log(PRINCIPAL, f"cases/{case_id}/planner/log.jsonl", event, **fields)


def flow_for_agents(bundle: HarnessBundle, agent_ids: list[str], hint: str | None = None) -> str:
    """Pick the flow a deterministic (non-LLM) agent selection belongs to."""
    if hint in bundle.flows and all(a in bundle.flows[hint]["allowed_agents"] for a in agent_ids):
        return hint
    if len(agent_ids) == 1:
        for name, f in bundle.flows.items():
            if agent_ids[0] in f.get("default_agents", []):
                return name
    for name, f in bundle.flows.items():
        if all(a in f["allowed_agents"] for a in agent_ids) and len(agent_ids) <= f["max_agents"]:
            return name
    return "multi"


async def understand_case(bundle: HarnessBundle, store: GuardedStore, case_id: str) -> CaseUnderstanding:
    cs = state.get(case_id)
    case = cs.case
    max_calls = bundle.guardrails["budgets"]["max_llm_calls_per_case"]
    with telemetry.span("planner_agent.understanding", "AGENT", **{"roa.case_id": case_id}) as sp:
        try:
            data, meta = await llm.call_structured(
                case_id, "understanding", bundle.model_for("understanding"), bundle.understanding_prompt,
                f"Case description:\n{case.description}", bundle.understanding_schema, max_calls=max_calls,
                timeout=bundle.manifest["llm"]["timeout_s"], **bundle.llm_opts("understanding"))
            u = CaseUnderstanding.model_validate(data)
            _log(store, case_id, "understanding", model=meta.model, output=u.model_dump(mode="json"))
        except llm.LLMError as e:
            fb = bundle.fallback["understanding_failure"]
            u = CaseUnderstanding(summary=case.description[:300], time_horizon=TimeHorizon.UNKNOWN)
            sp.set_attribute("roa.fallback", fb["action"])
            _log(store, case_id, "understanding_fallback", action=fb["action"], error=str(e)[:300])
        if case.property_name:
            u.property_name = case.property_name  # explicit intake value beats an LLM guess
        state.mutate(case_id, lambda s: setattr(s, "understanding", u))
        return u


async def plan_case(bundle: HarnessBundle, store: GuardedStore, case_id: str) -> PlanOutcome:
    cs = state.get(case_id)
    u = cs.understanding
    max_calls = bundle.guardrails["budgets"]["max_llm_calls_per_case"]
    events: list[GuardrailEvent] = []

    with telemetry.span("planner_agent.plan", "AGENT", **{"roa.case_id": case_id}) as sp:
        user = (f"Case description:\n{cs.case.description}\n\n"
                f"Understanding: {u.model_dump_json() if u else '{}'}\n\n"
                f"FLOWS:\n{bundle.flow_menu()}\n\nAGENT MENU:\n{bundle.agent_menu()}")
        llm_error = ""
        try:
            data, meta = await llm.call_structured(
                case_id, "planner", bundle.model_for("planner"), bundle.planner_prompt, user,
                bundle.planner_schema, max_calls=max_calls, timeout=bundle.manifest["llm"]["timeout_s"],
                **bundle.llm_opts("planner"))
            ordered, ev = _guard_plan(bundle, data["case_type"], data["agent_ids"], cs.case.description,
                                      u.case_type_hint if u else None)
            events += ev
            _log(store, case_id, "plan_proposed", model=meta.model, raw=data, guardrails_passed=not guardrails.failed(ev))
            if not guardrails.failed(ev):
                sp.set_attribute("roa.plan.source", "llm")
                return PlanOutcome(Plan(case_type=data["case_type"], agent_ids=ordered, reasoning=data["reasoning"], source="llm"), events)
            llm_error = "plan guardrails failed: " + "; ".join(f"{e.rule}: {e.detail}" for e in guardrails.failed(ev))
        except llm.LLMError as e:
            llm_error = str(e)
        _log(store, case_id, "planner_failed", error=llm_error[:400])

        # Fallback chain from harness/fallback/fallback.json: keyword match, then human.
        chain = bundle.fallback["planner_failure"]["chain"]
        if "keyword_match" in chain:
            matched = bundle.keyword_match(cs.case.description)
            if matched:
                case_type = flow_for_agents(bundle, matched, u.case_type_hint if u else None)
                ordered, ev = _guard_plan(bundle, case_type, matched)
                events += ev
                if not guardrails.failed(ev):
                    sp.set_attribute("roa.plan.source", "keyword_fallback")
                    _log(store, case_id, "plan_keyword_fallback", case_type=case_type, agent_ids=ordered)
                    return PlanOutcome(Plan(case_type=case_type, agent_ids=ordered,
                                            reasoning=f"Planner unavailable ({llm_error[:120]}); matched agent keywords.",
                                            source="keyword_fallback"), events)
        sp.set_attribute("roa.plan.source", "none")
        return PlanOutcome(None, events, reason=f"No valid plan could be produced: {llm_error[:300] or 'no agent keywords matched'}")


def _guard_plan(bundle: HarnessBundle, case_type: str, agent_ids: list[str], case_text: str | None = None,
                hint: str | None = None):
    with telemetry.span("guardrail.plan", "GUARDRAIL") as sp:
        ordered, ev = guardrails.check_plan(case_type, agent_ids, bundle, case_text, hint)
        bad = guardrails.failed(ev)
        sp.set_attribute("roa.guardrail.passed", not bad)
        if bad:
            sp.set_attribute("roa.guardrail.failed_rules", json.dumps([e.rule for e in bad]))
        return ordered, ev


async def run_planner_agent(bundle: HarnessBundle, store: GuardedStore, case_id: str) -> PlanOutcome:
    with telemetry.span("planner_agent", "AGENT", **{"roa.case_id": case_id}) as sp:
        u = await understand_case(bundle, store, case_id)
        sp.set_attribute("roa.understanding.hint", u.case_type_hint or "")
        sp.set_attribute("roa.understanding.horizon", u.time_horizon.value)
        outcome = await plan_case(bundle, store, case_id)
        sp.set_attribute("roa.plan.status", "ok" if outcome.plan else "needs_human")
        if outcome.plan:
            sp.set_attribute("roa.plan.case_type", outcome.plan.case_type)
            sp.set_attribute("roa.plan.source", outcome.plan.source)
            sp.set_attribute("roa.plan.agents", json.dumps(outcome.plan.agent_ids))
        state.add_guardrail_events(case_id, outcome.events)
        if outcome.plan is not None:
            state.mutate(case_id, lambda s: setattr(s, "plan", outcome.plan))
            _log(store, case_id, "plan_final", plan=outcome.plan.model_dump(mode="json"))
        return outcome
