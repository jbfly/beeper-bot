from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import AppConfig
from .llm import AskResponse, LlmClient, ask_archive


DEFAULT_EVAL_SUITE_PATH = Path(__file__).resolve().parents[2] / "eval" / "starter.json"


@dataclass(slots=True)
class EvalCase:
    case_id: str
    question: str
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    enabled: bool = True
    score_case: bool = True
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
    passed: bool
    elapsed_ms: int
    checks: dict[str, bool]
    failures: list[str]
    answer: str
    evidence_count: int
    evidence: list[dict[str, Any]]
    retrieval: list[dict[str, Any]]
    plan: dict[str, Any]


@dataclass(slots=True)
class EvalSuiteResult:
    suite_name: str
    description: str
    total_cases: int
    enabled_cases: int
    scored_cases: int
    passed_cases: int
    failed_cases: int
    results: list[EvalCaseResult]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


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
            )
        )

    return EvalSuite(
        name=str(payload.get("name") or suite_path.stem),
        description=str(payload.get("description") or "").strip(),
        cases=cases,
    )


def _contains(text: str, needle: str) -> bool:
    return needle.casefold() in text.casefold()


def _check_case(case: EvalCase, response: AskResponse) -> tuple[dict[str, bool], list[str]]:
    answer = response.answer.strip()
    evidence = response.evidence
    checks: dict[str, bool] = {}
    failures: list[str] = []

    checks["min_evidence"] = len(evidence) >= max(0, case.min_evidence)
    if not checks["min_evidence"]:
        failures.append(f"needed at least {case.min_evidence} evidence item(s), got {len(evidence)}")

    checks["citation_required"] = True
    if case.require_citation:
        checks["citation_required"] = "[" in answer and "]" in answer
        if not checks["citation_required"]:
            failures.append("answer has no citation")

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

    return checks, failures


def evaluate_case(config: AppConfig, case: EvalCase, llm_client: LlmClient | None = None) -> EvalCaseResult:
    started = time.perf_counter()
    response = ask_archive(config, case.question, llm_client=llm_client)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    checks, failures = _check_case(case, response)
    passed = all(checks.values()) if case.score_case and case.enabled else False

    return EvalCaseResult(
        case_id=case.case_id,
        question=case.question,
        tags=list(case.tags),
        notes=case.notes,
        enabled=case.enabled,
        score_case=case.score_case,
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
    )


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
        if case.enabled and case.score_case:
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
        results=results,
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
        "results": [asdict(item) for item in result.results],
    }


def format_suite_result(result: EvalSuiteResult) -> str:
    lines = [
        f"Suite: {result.suite_name}",
        f"Cases: total={result.total_cases} enabled={result.enabled_cases} scored={result.scored_cases} passed={result.passed_cases} failed={result.failed_cases}",
    ]
    if result.description:
        lines.insert(1, result.description)

    for item in result.results:
        if not item.enabled:
            status = "SKIP"
        elif not item.score_case:
            status = "INFO"
        else:
            status = "PASS" if item.passed else "FAIL"
        tags = f" [{' '.join(item.tags)}]" if item.tags else ""
        lines.append("")
        lines.append(f"{status} {item.case_id}{tags} ({item.elapsed_ms} ms)")
        lines.append(f"Q: {item.question}")
        lines.append(f"A: {item.answer}")
        if item.failures:
            for failure in item.failures:
                lines.append(f"- {failure}")
        if item.evidence:
            top = item.evidence[0]
            lines.append(f"Top evidence: {top['chat_name']} — {top['sender_name']} — {top['timestamp']}")
    return "\n".join(lines)
