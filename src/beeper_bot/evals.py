from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .config import AppConfig
from .llm import AskResponse, LlmClient, ask_archive


TOKEN_RE = __import__("re").compile(r"[A-Za-zÀ-ÿ0-9'.-]+")
SOURCE_TOKEN_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "from", "with",
    "what", "who", "is", "was", "were", "did", "does", "do", "my", "your", "our",
    "me", "you", "they", "them", "their", "that", "this", "those", "these", "again",
}


DEFAULT_EVAL_SUITE_PATH = Path(__file__).resolve().parents[2] / "eval" / "starter.json"


@dataclass(slots=True)
class EvalCase:
    case_id: str
    question: str
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    enabled: bool = True
    score_case: bool = True
    metrics_only: bool = False
    min_evidence: int = 1
    require_citation: bool = True
    answer_contains_all: list[str] = field(default_factory=list)
    answer_contains_any: list[str] = field(default_factory=list)
    answer_contains_pool: list[str] = field(default_factory=list)
    answer_contains_count: int = 0
    answer_not_contains: list[str] = field(default_factory=list)
    evidence_sender_any: list[str] = field(default_factory=list)
    evidence_chat_any: list[str] = field(default_factory=list)
    top_evidence_sender_any: list[str] = field(default_factory=list)
    top_evidence_chat_any: list[str] = field(default_factory=list)
    plan_people_any: list[str] = field(default_factory=list)
    plan_people_all: list[str] = field(default_factory=list)
    plan_preferred_sender_any: list[str] = field(default_factory=list)
    plan_preferred_chat_any: list[str] = field(default_factory=list)
    plan_answer_kind_any: list[str] = field(default_factory=list)
    plan_time_hint_any: list[str] = field(default_factory=list)
    control_turns: list[dict[str, Any]] = field(default_factory=list)
    memory_state: dict[str, Any] = field(default_factory=dict)
    expected_actions: list[str] = field(default_factory=list)
    expected_sources: list[str] = field(default_factory=list)
    expected_path: str = ""
    context_budget_class: str = ""


@dataclass(slots=True)
class EvalSuite:
    name: str
    description: str = ""
    cases: list[EvalCase] = field(default_factory=list)


@dataclass(slots=True)
class EvalCaseResult:
    case_id: str
    question: str
    tags: list[str]
    notes: str
    enabled: bool
    score_case: bool
    metrics_only: bool
    passed: bool
    elapsed_ms: int
    checks: dict[str, bool]
    failures: list[str]
    answer: str
    evidence_count: int
    evidence: list[dict[str, Any]]
    retrieval: list[dict[str, Any]]
    plan: dict[str, Any]
    control_turns: list[dict[str, Any]]
    memory_state: dict[str, Any]
    expected_actions: list[str]
    inferred_actions: list[str]
    expected_sources: list[str]
    inferred_sources: list[str]
    expected_path: str
    answer_path: str
    context_budget_class: str


@dataclass(slots=True)
class EvalFamilySummary:
    family_id: str
    scored_cases: int
    passed_cases: int
    failed_cases: int
    first_failed_rung: str
    rung_results: dict[str, dict[str, Any]]


@dataclass(slots=True)
class EvalSuiteResult:
    suite_name: str
    description: str
    total_cases: int
    enabled_cases: int
    scored_cases: int
    passed_cases: int
    failed_cases: int
    runtime: dict[str, Any]
    results: list[EvalCaseResult]
    family_summaries: list[EvalFamilySummary] = field(default_factory=list)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]



def _dict_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return dict(value)



def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def load_eval_suite(path: Path | str) -> EvalSuite:
    suite_path = Path(path).expanduser()
    payload = json.loads(suite_path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid eval suite: {suite_path}")
    raw_cases = payload.get("cases", [])
    if not isinstance(raw_cases, list):
        raise ValueError(f"Invalid eval suite cases: {suite_path}")

    cases: list[EvalCase] = []
    for item in raw_cases:
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("id") or "").strip()
        question = str(item.get("question") or "").strip()
        if not case_id or not question:
            continue
        cases.append(
            EvalCase(
                case_id=case_id,
                question=question,
                tags=_string_list(item.get("tags")),
                notes=str(item.get("notes") or "").strip(),
                enabled=bool(item.get("enabled", True)),
                score_case=bool(item.get("score_case", True)),
                metrics_only=bool(item.get("metrics_only", False)),
                min_evidence=int(item.get("min_evidence", 1)),
                require_citation=bool(item.get("require_citation", True)),
                answer_contains_all=_string_list(item.get("answer_contains_all")),
                answer_contains_any=_string_list(item.get("answer_contains_any")),
                answer_contains_pool=_string_list(item.get("answer_contains_pool")),
                answer_contains_count=int(item.get("answer_contains_count", 0)),
                answer_not_contains=_string_list(item.get("answer_not_contains")),
                evidence_sender_any=_string_list(item.get("evidence_sender_any")),
                evidence_chat_any=_string_list(item.get("evidence_chat_any")),
                top_evidence_sender_any=_string_list(item.get("top_evidence_sender_any")),
                top_evidence_chat_any=_string_list(item.get("top_evidence_chat_any")),
                plan_people_any=_string_list(item.get("plan_people_any")),
                plan_people_all=_string_list(item.get("plan_people_all")),
                plan_preferred_sender_any=_string_list(item.get("plan_preferred_sender_any")),
                plan_preferred_chat_any=_string_list(item.get("plan_preferred_chat_any")),
                plan_answer_kind_any=_string_list(item.get("plan_answer_kind_any")),
                plan_time_hint_any=_string_list(item.get("plan_time_hint_any")),
                control_turns=_dict_list(item.get("control_turns")),
                memory_state=_dict_value(item.get("memory_state")),
                expected_actions=_string_list(item.get("expected_actions")),
                expected_sources=_string_list(item.get("expected_sources")),
                expected_path=str(item.get("expected_path") or "").strip().casefold(),
                context_budget_class=str(item.get("context_budget_class") or "").strip(),
            )
        )

    return EvalSuite(
        name=str(payload.get("name") or suite_path.stem),
        description=str(payload.get("description") or "").strip(),
        cases=cases,
    )


def _contains(text: str, needle: str) -> bool:
    return needle.casefold() in text.casefold()


def _tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in TOKEN_RE.findall(text)
        if len(token) > 2 and token.casefold() not in SOURCE_TOKEN_STOPWORDS
    }


def _has_overlap(answer_tokens: set[str], text: str, threshold: int = 2) -> bool:
    return len(answer_tokens & _tokens(text)) >= threshold


def _infer_answer_actions(case: EvalCase, response: AskResponse) -> list[str]:
    answer = response.answer.strip().casefold()
    inferred: list[str] = []

    proposed = getattr(response, "proposed_action", None)
    if isinstance(proposed, dict):
        kind = str(proposed.get("kind") or "").strip()
        if kind:
            inferred.append(kind)
            inferred.append("confirm-memory-write")

    confirm_markers = (
        "confirm",
        "please confirm",
        "before i save",
        "should i save",
        "do you want me to save",
        "do you want me to remember",
        "can save that",
    )
    if any(marker in answer for marker in confirm_markers):
        inferred.append("confirm-memory-write")

    alias_markers = (
        "alias",
        "remember that",
        "alias for",
        "refers to",
    )
    if any(marker in answer for marker in alias_markers):
        inferred.append("add-alias")

    person_markers = (
        "add person",
        "new person",
        "create person",
    )
    if any(marker in answer for marker in person_markers):
        inferred.append("add-person")

    relationship_markers = (
        "relationship",
        "sister",
        "brother in law",
        "girlfriend",
        "wife",
        "husband",
    )
    if any(marker in answer for marker in relationship_markers) and ("remember" in answer or "save" in answer):
        inferred.append("add-relationship-fact")

    ordered: list[str] = []
    seen: set[str] = set()
    for value in inferred:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(value)
    return ordered


def _infer_answer_sources(case: EvalCase, response: AskResponse) -> list[str]:
    answer = response.answer.strip()
    answer_tokens = _tokens(answer)
    inferred: list[str] = list(getattr(response, "used_sources", []) or [])

    if "[" in answer and "]" in answer:
        inferred.append("archive")

    facts = case.memory_state.get("facts") if isinstance(case.memory_state, dict) else None
    if isinstance(facts, list):
        for item in facts:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or "").strip()
            predicate = str(item.get("predicate") or "").strip()
            obj = str(item.get("object") or "").strip()
            if any(_contains(answer, value) for value in (subject, predicate, obj) if value):
                inferred.append("memory")
                break
        else:
            if "memory" in answer.casefold() or "structured memory" in answer.casefold():
                inferred.append("memory")

    summary = str(case.memory_state.get("control_summary") or "").strip() if isinstance(case.memory_state, dict) else ""
    if summary and _has_overlap(answer_tokens, summary):
        inferred.append("summary")

    if case.control_turns:
        for turn in case.control_turns:
            content = str(turn.get("content") or "").strip()
            if content and _has_overlap(answer_tokens, content):
                inferred.append("control-turns")
                break

    ordered: list[str] = []
    seen: set[str] = set()
    for value in inferred:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(value)
    return ordered


def _check_case(case: EvalCase, response: AskResponse) -> tuple[dict[str, bool], list[str], list[str], list[str]]:
    answer = response.answer.strip()
    evidence = response.evidence
    checks: dict[str, bool] = {}
    failures: list[str] = []

    inferred_actions = _infer_answer_actions(case, response)

    answer_path = getattr(response, "answer_path", "model") or "model"
    checks["answer_path"] = True
    if case.expected_path:
        checks["answer_path"] = answer_path == case.expected_path
        if not checks["answer_path"]:
            failures.append(f"answer path was '{answer_path}', expected '{case.expected_path}'")

    checks["min_evidence"] = len(evidence) >= max(0, case.min_evidence)
    if not checks["min_evidence"]:
        failures.append(f"needed at least {case.min_evidence} evidence item(s), got {len(evidence)}")

    inferred_sources = _infer_answer_sources(case, response)
    requires_archive_citation = case.require_citation and (not case.expected_sources or "archive" in {value.casefold() for value in case.expected_sources})
    checks["citation_required"] = True
    if requires_archive_citation:
        checks["citation_required"] = "[" in answer and "]" in answer
        if not checks["citation_required"]:
            failures.append("answer has no citation")

    checks["source_classes_expected"] = True
    if case.expected_sources:
        expected = {value.casefold() for value in case.expected_sources}
        found = {value.casefold() for value in inferred_sources}
        checks["source_classes_expected"] = expected.issubset(found)
        if not checks["source_classes_expected"]:
            missing = sorted(expected - found)
            failures.append("answer missing expected source classes: " + ", ".join(missing))

    checks["source_classes_no_fake_archive"] = True
    if case.expected_sources and "archive" not in {value.casefold() for value in case.expected_sources}:
        checks["source_classes_no_fake_archive"] = "archive" not in {value.casefold() for value in inferred_sources}
        if not checks["source_classes_no_fake_archive"]:
            failures.append("answer used archive-style citation where archive source was not expected")

    checks["expected_actions"] = True
    if case.expected_actions:
        expected = {value.casefold() for value in case.expected_actions}
        found = {value.casefold() for value in inferred_actions}
        checks["expected_actions"] = expected.issubset(found)
        if not checks["expected_actions"]:
            missing = sorted(expected - found)
            failures.append("answer missing expected actions: " + ", ".join(missing))

    checks["answer_contains_all"] = all(_contains(answer, token) for token in case.answer_contains_all)
    if case.answer_contains_all and not checks["answer_contains_all"]:
        failures.append("answer missing required text: " + ", ".join(case.answer_contains_all))

    checks["answer_contains_any"] = True
    if case.answer_contains_any:
        checks["answer_contains_any"] = any(_contains(answer, token) for token in case.answer_contains_any)
        if not checks["answer_contains_any"]:
            failures.append("answer missing any-of text: " + ", ".join(case.answer_contains_any))

    checks["answer_contains_pool"] = True
    if case.answer_contains_pool:
        found = sum(1 for token in case.answer_contains_pool if _contains(answer, token))
        needed = case.answer_contains_count or 1
        checks["answer_contains_pool"] = found >= needed
        if not checks["answer_contains_pool"]:
            failures.append(f"answer matched {found}/{needed} pooled text fragments")

    checks["answer_not_contains"] = all(not _contains(answer, token) for token in case.answer_not_contains)
    if case.answer_not_contains and not checks["answer_not_contains"]:
        failures.append("answer contains forbidden text: " + ", ".join(case.answer_not_contains))

    checks["evidence_sender_any"] = True
    if case.evidence_sender_any:
        checks["evidence_sender_any"] = any(
            any(_contains(item.sender_name, token) for token in case.evidence_sender_any)
            for item in evidence
        )
        if not checks["evidence_sender_any"]:
            failures.append("evidence sender mismatch: expected one of " + ", ".join(case.evidence_sender_any))

    checks["evidence_chat_any"] = True
    if case.evidence_chat_any:
        checks["evidence_chat_any"] = any(
            any(_contains(item.chat_name, token) for token in case.evidence_chat_any)
            for item in evidence
        )
        if not checks["evidence_chat_any"]:
            failures.append("evidence chat mismatch: expected one of " + ", ".join(case.evidence_chat_any))

    top = evidence[0] if evidence else None
    checks["top_evidence_sender_any"] = True
    if case.top_evidence_sender_any:
        checks["top_evidence_sender_any"] = bool(top) and any(
            _contains(top.sender_name, token) for token in case.top_evidence_sender_any
        )
        if not checks["top_evidence_sender_any"]:
            failures.append("top evidence sender mismatch: expected one of " + ", ".join(case.top_evidence_sender_any))

    checks["top_evidence_chat_any"] = True
    if case.top_evidence_chat_any:
        checks["top_evidence_chat_any"] = bool(top) and any(
            _contains(top.chat_name, token) for token in case.top_evidence_chat_any
        )
        if not checks["top_evidence_chat_any"]:
            failures.append("top evidence chat mismatch: expected one of " + ", ".join(case.top_evidence_chat_any))

    plan = response.plan
    checks["plan_people_any"] = True
    if case.plan_people_any:
        checks["plan_people_any"] = any(
            any(_contains(value, token) for token in case.plan_people_any)
            for value in plan.people
        )
        if not checks["plan_people_any"]:
            failures.append("plan people mismatch: expected one of " + ", ".join(case.plan_people_any))

    checks["plan_people_all"] = True
    if case.plan_people_all:
        checks["plan_people_all"] = all(
            any(_contains(value, token) for value in plan.people)
            for token in case.plan_people_all
        )
        if not checks["plan_people_all"]:
            failures.append("plan people missing required entries: " + ", ".join(case.plan_people_all))

    checks["plan_preferred_sender_any"] = True
    if case.plan_preferred_sender_any:
        checks["plan_preferred_sender_any"] = any(
            any(_contains(value, token) for token in case.plan_preferred_sender_any)
            for value in plan.preferred_senders
        )
        if not checks["plan_preferred_sender_any"]:
            failures.append("plan preferred sender mismatch: expected one of " + ", ".join(case.plan_preferred_sender_any))

    checks["plan_preferred_chat_any"] = True
    if case.plan_preferred_chat_any:
        checks["plan_preferred_chat_any"] = any(
            any(_contains(value, token) for token in case.plan_preferred_chat_any)
            for value in plan.preferred_chats
        )
        if not checks["plan_preferred_chat_any"]:
            failures.append("plan preferred chat mismatch: expected one of " + ", ".join(case.plan_preferred_chat_any))

    checks["plan_answer_kind_any"] = True
    if case.plan_answer_kind_any:
        checks["plan_answer_kind_any"] = any(_contains(plan.answer_kind, token) for token in case.plan_answer_kind_any)
        if not checks["plan_answer_kind_any"]:
            failures.append("plan answer_kind mismatch: expected one of " + ", ".join(case.plan_answer_kind_any))

    checks["plan_time_hint_any"] = True
    if case.plan_time_hint_any:
        checks["plan_time_hint_any"] = any(_contains(plan.time_hint, token) for token in case.plan_time_hint_any)
        if not checks["plan_time_hint_any"]:
            failures.append("plan time_hint mismatch: expected one of " + ", ".join(case.plan_time_hint_any))

    return checks, failures, inferred_sources, inferred_actions


def evaluate_case(config: AppConfig, case: EvalCase, llm_client: LlmClient | None = None) -> EvalCaseResult:
    started = time.perf_counter()
    response = ask_archive(
        config,
        case.question,
        llm_client=llm_client,
        control_turns=case.control_turns,
        memory_state=case.memory_state,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    checks, failures, inferred_sources, inferred_actions = _check_case(case, response)
    passed = all(checks.values()) if case.score_case and case.enabled and not case.metrics_only else False

    return EvalCaseResult(
        case_id=case.case_id,
        question=case.question,
        tags=list(case.tags),
        notes=case.notes,
        enabled=case.enabled,
        score_case=case.score_case,
        metrics_only=case.metrics_only,
        passed=passed,
        elapsed_ms=elapsed_ms,
        checks=checks,
        failures=failures,
        answer=response.answer,
        evidence_count=len(response.evidence),
        evidence=[
            {
                "citation_id": item.citation_id,
                "message_id": item.message_id,
                "chat_id": item.chat_id,
                "chat_name": item.chat_name,
                "sender_name": item.sender_name,
                "timestamp": item.timestamp,
                "excerpt": item.excerpt,
                "score": item.score,
            }
            for item in response.evidence
        ],
        retrieval=[
            {
                "message_id": item.message_id,
                "chat_id": item.chat_id,
                "chat_name": item.chat_name,
                "sender_name": item.sender_name,
                "timestamp": item.timestamp,
                "text": item.text,
                "score": item.score,
                "match_reasons": list(item.match_reasons),
            }
            for item in response.retrieval.results
        ],
        plan={
            "normalized_question": response.plan.normalized_question,
            "search_queries": list(response.plan.search_queries),
            "people": list(response.plan.people),
            "chat_hints": list(response.plan.chat_hints),
            "preferred_senders": list(response.plan.preferred_senders),
            "preferred_chats": list(response.plan.preferred_chats),
            "answer_kind": response.plan.answer_kind,
            "time_hint": response.plan.time_hint,
        },
        control_turns=list(case.control_turns),
        memory_state=dict(case.memory_state),
        expected_actions=list(case.expected_actions),
        inferred_actions=inferred_actions,
        expected_sources=list(case.expected_sources),
        inferred_sources=inferred_sources,
        expected_path=case.expected_path,
        answer_path=getattr(response, "answer_path", "model") or "model",
        context_budget_class=case.context_budget_class,
    )


def configure_eval_run(
    config: AppConfig,
    *,
    deterministic: bool = False,
    temperature: float | None = None,
    planner_temperature: float | None = None,
) -> AppConfig:
    llm = config.llm
    answer_temperature = temperature
    selected_planner_temperature = planner_temperature

    if deterministic:
        if answer_temperature is None:
            answer_temperature = 0.0
        if selected_planner_temperature is None:
            selected_planner_temperature = 0.0

    if answer_temperature is None and selected_planner_temperature is None:
        return config

    return replace(
        config,
        llm=replace(
            llm,
            temperature=llm.temperature if answer_temperature is None else answer_temperature,
            planner_temperature=llm.planner_temperature if selected_planner_temperature is None else selected_planner_temperature,
        ),
    )



def _eval_runtime_payload(config: AppConfig) -> dict[str, Any]:
    return {
        "model": config.llm.model,
        "base_url": config.llm.base_url,
        "temperature": config.llm.temperature,
        "planner_temperature": config.llm.planner_temperature,
        "max_output_tokens": config.llm.max_output_tokens,
        "planner_max_output_tokens": config.llm.planner_max_output_tokens,
    }



RUNG_ORDER = {"short": 0, "medium": 1, "long": 2, "stress": 3}


def _case_family_id(result: EvalCaseResult) -> str:
    budget = result.context_budget_class.strip().casefold()
    suffix = f"_{budget}" if budget else ""
    if suffix and result.case_id.casefold().endswith(suffix):
        return result.case_id[: -len(suffix)]
    return result.case_id


def _build_family_summaries(results: list[EvalCaseResult]) -> list[EvalFamilySummary]:
    grouped: dict[str, list[EvalCaseResult]] = {}
    for result in results:
        if not result.enabled:
            continue
        budget = result.context_budget_class.strip().casefold()
        if not budget:
            continue
        family_id = _case_family_id(result)
        grouped.setdefault(family_id, []).append(result)

    summaries: list[EvalFamilySummary] = []
    for family_id in sorted(grouped):
        items = sorted(grouped[family_id], key=lambda item: (RUNG_ORDER.get(item.context_budget_class.casefold(), 99), item.case_id))
        rung_results: dict[str, dict[str, Any]] = {}
        scored_cases = 0
        passed_cases = 0
        failed_cases = 0
        first_failed_rung = ""
        for item in items:
            budget = item.context_budget_class
            scored = item.enabled and item.score_case and not item.metrics_only
            rung_results[budget] = {
                "case_id": item.case_id,
                "scored": scored,
                "metrics_only": item.metrics_only,
                "passed": item.passed if scored else False,
                "elapsed_ms": item.elapsed_ms,
            }
            if not scored:
                continue
            scored_cases += 1
            if item.passed:
                passed_cases += 1
            else:
                failed_cases += 1
                if not first_failed_rung:
                    first_failed_rung = budget
        summaries.append(
            EvalFamilySummary(
                family_id=family_id,
                scored_cases=scored_cases,
                passed_cases=passed_cases,
                failed_cases=failed_cases,
                first_failed_rung=first_failed_rung,
                rung_results=rung_results,
            )
        )
    return summaries


def run_eval_suite(
    config: AppConfig,
    suite: EvalSuite,
    llm_client: LlmClient | None = None,
    case_ids: list[str] | None = None,
    tags: list[str] | None = None,
) -> EvalSuiteResult:
    selected_ids = {value.casefold() for value in case_ids or [] if value.strip()}
    selected_tags = {value.casefold() for value in tags or [] if value.strip()}

    results: list[EvalCaseResult] = []
    enabled_cases = 0
    scored_cases = 0
    passed_cases = 0
    failed_cases = 0

    for case in suite.cases:
        if selected_ids and case.case_id.casefold() not in selected_ids:
            continue
        if selected_tags and not any(tag.casefold() in selected_tags for tag in case.tags):
            continue
        result = evaluate_case(config, case, llm_client=llm_client)
        results.append(result)
        if case.enabled:
            enabled_cases += 1
        if case.enabled and case.score_case and not case.metrics_only:
            scored_cases += 1
            if result.passed:
                passed_cases += 1
            else:
                failed_cases += 1

    return EvalSuiteResult(
        suite_name=suite.name,
        description=suite.description,
        total_cases=len(results),
        enabled_cases=enabled_cases,
        scored_cases=scored_cases,
        passed_cases=passed_cases,
        failed_cases=failed_cases,
        runtime=_eval_runtime_payload(config),
        results=results,
        family_summaries=_build_family_summaries(results),
    )


def suite_result_to_dict(result: EvalSuiteResult) -> dict[str, Any]:
    return {
        "suite_name": result.suite_name,
        "description": result.description,
        "total_cases": result.total_cases,
        "enabled_cases": result.enabled_cases,
        "scored_cases": result.scored_cases,
        "passed_cases": result.passed_cases,
        "failed_cases": result.failed_cases,
        "runtime": result.runtime,
        "results": [asdict(item) for item in result.results],
        "family_summaries": [asdict(item) for item in result.family_summaries],
    }


def format_suite_result(result: EvalSuiteResult) -> str:
    lines = [
        f"Suite: {result.suite_name}",
        f"Cases: total={result.total_cases} enabled={result.enabled_cases} scored={result.scored_cases} passed={result.passed_cases} failed={result.failed_cases}",
    ]
    if result.description:
        lines.insert(1, result.description)

    if result.runtime:
        lines.append(
            "Runtime: "
            f"model={result.runtime.get('model')} "
            f"temperature={result.runtime.get('temperature')} "
            f"planner_temperature={result.runtime.get('planner_temperature')}"
        )

    if result.family_summaries:
        lines.append("Family summary:")
        for family in result.family_summaries:
            first_failed = family.first_failed_rung or "none"
            rung_bits = []
            for rung in sorted(family.rung_results, key=lambda value: RUNG_ORDER.get(value.casefold(), 99)):
                rung_result = family.rung_results[rung]
                if rung_result["metrics_only"]:
                    status = "metric"
                elif not rung_result["scored"]:
                    status = "info"
                else:
                    status = "pass" if rung_result["passed"] else "fail"
                rung_bits.append(f"{rung}={status}")
            lines.append(
                f"- {family.family_id}: passed={family.passed_cases}/{family.scored_cases} "
                f"first_failed_rung={first_failed} ({', '.join(rung_bits)})"
            )

    for item in result.results:
        if not item.enabled:
            status = "SKIP"
        elif item.metrics_only:
            status = "METRIC"
        elif not item.score_case:
            status = "INFO"
        else:
            status = "PASS" if item.passed else "FAIL"
        tags = f" [{' '.join(item.tags)}]" if item.tags else ""
        lines.append("")
        lines.append(f"{status} {item.case_id}{tags} ({item.elapsed_ms} ms)")
        lines.append(f"Q: {item.question}")
        lines.append(
            "Plan: "
            f"kind={item.plan['answer_kind']} time={item.plan['time_hint']} "
            f"people={item.plan['people']} senders={item.plan['preferred_senders']} chats={item.plan['preferred_chats']}"
        )
        if item.expected_sources:
            lines.append(f"Sources: expected={item.expected_sources} inferred={item.inferred_sources}")
        if item.expected_actions:
            lines.append(f"Actions: expected={item.expected_actions} inferred={item.inferred_actions}")
        lines.append(f"A: {item.answer}")
        if item.failures:
            for failure in item.failures:
                lines.append(f"- {failure}")
        if item.evidence:
            top = item.evidence[0]
            lines.append(f"Top evidence: {top['chat_name']} — {top['sender_name']} — {top['timestamp']}")
    return "\n".join(lines)
