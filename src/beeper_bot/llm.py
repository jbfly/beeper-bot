from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import error, parse, request

from .config import AppConfig
from .planning import QueryPlan
from .retrieval import SearchCatalog, SearchResponse, SearchResult, collect_search_catalog, search_archive_multi


CITATION_RE = re.compile(r"\[(\d+)\]")
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class LlmError(RuntimeError):
    pass


@dataclass(slots=True)
class EvidenceItem:
    citation_id: str
    message_id: str
    chat_id: str
    chat_name: str
    sender_name: str
    timestamp: str
    excerpt: str
    score: float


@dataclass(slots=True)
class AskResponse:
    question: str
    answer: str
    evidence: list[EvidenceItem]
    retrieval: SearchResponse
    plan: QueryPlan


class LlmClient(Protocol):
    def answer_from_evidence(self, config: AppConfig, question: str, evidence: list[EvidenceItem]) -> str: ...


class QueryPlannerClient(Protocol):
    def plan_query(self, config: AppConfig, question: str, catalog: SearchCatalog) -> QueryPlan: ...


def _require_loopback_base_url(base_url: str) -> None:
    parsed = parse.urlsplit(base_url)
    hostname = parsed.hostname
    if not hostname:
        raise LlmError(f"LLM base URL is invalid: {base_url}")
    if hostname == "localhost":
        return
    try:
        if ipaddress.ip_address(hostname).is_loopback:
            return
    except ValueError:
        pass
    raise LlmError(f"LLM base URL must be loopback for MVP: {base_url}")


class OpenAiCompatLlmClient:
    def _post_chat(self, config: AppConfig, messages: list[dict[str, str]], max_tokens: int | None = None) -> str:
        _require_loopback_base_url(config.llm.base_url)
        payload = {
            "model": config.llm.model,
            "messages": messages,
            "temperature": config.llm.temperature,
            "max_tokens": max_tokens or config.llm.max_output_tokens,
        }
        body = json.dumps(payload).encode("utf-8")
        url = f"{config.llm.base_url.rstrip('/')}/chat/completions"
        req = request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with request.urlopen(req, timeout=config.llm.timeout_seconds) as resp:
                response_body = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise LlmError(f"LLM API failed: HTTP {exc.code} {body_text}") from exc
        except error.URLError as exc:
            raise LlmError(f"LLM API failed: {exc}") from exc

        try:
            payload = json.loads(response_body)
            return str(payload["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LlmError("LLM API returned an unexpected response") from exc

    def answer_from_evidence(self, config: AppConfig, question: str, evidence: list[EvidenceItem]) -> str:
        prompt = build_answer_prompt(question, evidence)
        return self._post_chat(
            config,
            [
                {"role": "system", "content": "Answer only from provided evidence. No hidden reasoning."},
                {"role": "user", "content": prompt},
            ],
        )

    def plan_query(self, config: AppConfig, question: str, catalog: SearchCatalog) -> QueryPlan:
        prompt = build_planner_prompt(question, catalog)
        raw = self._post_chat(
            config,
            [
                {"role": "system", "content": "You plan archive retrieval queries. Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=min(400, config.llm.max_output_tokens),
        )
        return parse_query_plan(raw, question)


def build_evidence_packet(results: list[SearchResult], limit: int) -> list[EvidenceItem]:
    evidence: list[EvidenceItem] = []
    for idx, result in enumerate(results[:limit], start=1):
        excerpt = result.text.replace("\n", " ").strip()
        if len(excerpt) > 400:
            excerpt = excerpt[:397].rstrip() + "..."
        evidence.append(
            EvidenceItem(
                citation_id=f"[{idx}]",
                message_id=result.message_id,
                chat_id=result.chat_id,
                chat_name=result.chat_name,
                sender_name=result.sender_name or "unknown",
                timestamp=result.timestamp,
                excerpt=excerpt,
                score=result.score,
            )
        )
    return evidence


def build_planner_prompt(question: str, catalog: SearchCatalog) -> str:
    sender_block = ", ".join(catalog.sender_names[:60]) or "(none)"
    chat_block = ", ".join(catalog.chat_names[:60]) or "(none)"
    return (
        "Plan retrieval for a private local chat archive.\n"
        "Return one JSON object only. No markdown.\n"
        "Use these keys exactly: normalized_question, search_queries, people, chat_hints, preferred_senders, preferred_chats, answer_kind, time_hint.\n"
        "search_queries should contain 3 to 8 short search strings when possible.\n"
        "Expand nicknames, tense changes, synonyms, and likely paraphrases.\n"
        "If the user mentions one person who appears in several chats, search broadly across those chats.\n"
        "answer_kind must be one of: fact, date, url, last-message, summary.\n"
        "time_hint must be one of: recent, any.\n\n"
        f"Known sender names:\n{sender_block}\n\n"
        f"Known chat names:\n{chat_block}\n\n"
        f"User question:\n{question}\n"
    )


def parse_query_plan(raw: str, original_question: str) -> QueryPlan:
    match = JSON_OBJECT_RE.search(raw)
    if not match:
        raise LlmError("Query planner did not return JSON")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LlmError("Query planner returned invalid JSON") from exc

    def list_of_strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    normalized_question = str(payload.get("normalized_question") or original_question).strip()
    return QueryPlan(
        normalized_question=normalized_question or original_question,
        search_queries=list_of_strings(payload.get("search_queries")),
        people=list_of_strings(payload.get("people")),
        chat_hints=list_of_strings(payload.get("chat_hints")),
        preferred_senders=list_of_strings(payload.get("preferred_senders")),
        preferred_chats=list_of_strings(payload.get("preferred_chats")),
        answer_kind=str(payload.get("answer_kind") or "fact").strip() or "fact",
        time_hint=str(payload.get("time_hint") or "any").strip() or "any",
    )


def fallback_query_plan(question: str, catalog: SearchCatalog) -> QueryPlan:
    lowered = question.lower()
    answer_kind = "fact"
    if any(word in lowered for word in ["when", "anniversary", "birthday", "date"]):
        answer_kind = "date"
    elif any(word in lowered for word in ["url", "link", "website"]):
        answer_kind = "url"
    elif any(word in lowered for word in ["last message", "last thing", "most recent", "recently told", "last told"]):
        answer_kind = "last-message"
    elif any(word in lowered for word in ["projects", "working on", "summary", "discuss"]):
        answer_kind = "summary"

    time_hint = "recent" if any(word in lowered for word in ["last", "recent", "recently", "today", "yesterday"]) else "any"
    preferred_senders = [name for name in catalog.sender_names if name and name.casefold().split()[0] in lowered][:3]
    preferred_chats = [name for name in catalog.chat_names if name and name.casefold().split()[0] in lowered][:3]
    return QueryPlan(
        normalized_question=question,
        search_queries=[question],
        people=preferred_senders,
        chat_hints=preferred_chats,
        preferred_senders=preferred_senders,
        preferred_chats=preferred_chats,
        answer_kind=answer_kind,
        time_hint=time_hint,
    )


def plan_archive_query(config: AppConfig, question: str, llm_client: QueryPlannerClient | None = None) -> QueryPlan:
    catalog = collect_search_catalog(config)
    client = llm_client or OpenAiCompatLlmClient()
    try:
        plan = client.plan_query(config, question, catalog)
    except Exception:
        return fallback_query_plan(question, catalog)

    if not plan.search_queries:
        return fallback_query_plan(question, catalog)
    return plan


def build_answer_prompt(question: str, evidence: list[EvidenceItem]) -> str:
    evidence_block = "\n\n".join(
        (
            f"{item.citation_id} chat={item.chat_name}\n"
            f"sender={item.sender_name}\n"
            f"timestamp={item.timestamp}\n"
            f"excerpt={item.excerpt}"
        )
        for item in evidence
    )
    return (
        "Answer the question using only the evidence below.\n"
        "If the evidence is partial, say what the evidence does support and what remains unclear.\n"
        "Only say the evidence is insufficient when the evidence truly does not support even a partial answer.\n"
        "Cite factual claims with citation ids like [1].\n"
        "Do not invent names, dates, addresses, or events.\n"
        "Do not output chain-of-thought.\n\n"
        f"Question:\n{question}\n\n"
        f"Evidence:\n{evidence_block}"
    )


def _strip_invalid_citations(answer: str, evidence: list[EvidenceItem]) -> str:
    allowed = {item.citation_id[1:-1] for item in evidence}

    def replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1) in allowed else ""

    cleaned = CITATION_RE.sub(replace, answer)
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def format_ask_response(response: AskResponse) -> str:
    if not response.evidence:
        return "I could not find enough local evidence to answer that."

    answer = _strip_invalid_citations(response.answer.strip(), response.evidence)
    if not answer:
        answer = "I could not find enough local evidence to answer that."

    cited_ids = {f"[{match}]" for match in CITATION_RE.findall(answer)}
    source_items = [item for item in response.evidence if item.citation_id in cited_ids] or response.evidence

    lines = [answer, "", "Sources:"]
    for item in source_items:
        lines.append(f"{item.citation_id} {item.chat_name} — {item.sender_name} — {item.timestamp}")
    return "\n".join(lines)


def ask_archive(
    config: AppConfig,
    question: str,
    limit: int | None = None,
    llm_client: LlmClient | None = None,
) -> AskResponse:
    planner_client = llm_client if llm_client and hasattr(llm_client, "plan_query") else None
    plan = plan_archive_query(config, question, planner_client)  # type: ignore[arg-type]
    retrieval = search_archive_multi(
        config,
        plan.all_queries(question),
        limit=max(limit or config.llm.max_input_snippets, config.llm.max_input_snippets),
        preferred_senders=plan.preferred_senders or plan.people,
        preferred_chats=plan.preferred_chats or plan.chat_hints,
        answer_kind=plan.answer_kind,
        time_hint=plan.time_hint,
    )
    evidence = build_evidence_packet(retrieval.results, config.llm.max_input_snippets)
    if not evidence:
        return AskResponse(
            question=question,
            answer="I could not find enough local evidence to answer that.",
            evidence=[],
            retrieval=retrieval,
            plan=plan,
        )

    client = llm_client or OpenAiCompatLlmClient()
    answer = client.answer_from_evidence(config, question, evidence)
    return AskResponse(question=question, answer=answer, evidence=evidence, retrieval=retrieval, plan=plan)
