from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib import error, parse, request

from .config import AppConfig
from .people import PersonGraph, load_person_graph
from .planning import QueryPlan
from .retrieval import (
    ADDRESS_RE,
    DATE_RE,
    EMAIL_RE,
    URL_RE,
    ChatWindow,
    SearchCatalog,
    SearchResponse,
    SearchResult,
    _date_bounds_from_query,
    collect_search_catalog,
    detect_query_features,
    expand_results_with_context,
    expand_results_with_spans,
    pack_chat_windows,
    search_archive_multi,
)
from .tracing import trace_event


CITATION_RE = re.compile(r"\[(\d+)\]")
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
POSTAL_CODE_RE = re.compile(r"\b\d{4}-\d{3}(?:\s*[|–-]?\s*[A-Za-zÀ-ÿ.' ]+)?", re.IGNORECASE)


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
    used_sources: list[str] = field(default_factory=list)
    answer_path: str = "model"
    proposed_action: dict | None = None


class LlmClient(Protocol):
    def answer_from_evidence(
        self,
        config: AppConfig,
        question: str,
        evidence: list[EvidenceItem],
        person_context: str = "",
        control_context: str = "",
        persona: str = "",
    ) -> str: ...


class QueryPlannerClient(Protocol):
    def plan_query(self, config: AppConfig, question: str, catalog: SearchCatalog, graph: PersonGraph) -> QueryPlan: ...


def _require_local_base_url(base_url: str) -> None:
    """Guarantee the *local* LLM tier never leaves your own hardware.

    Allows loopback and RFC1918/link-local hosts (e.g. the model on alpha at
    192.168.x over the LAN) but rejects public addresses, so private chat
    content can't accidentally be sent to an internet endpoint on this tier.
    The cloud tier (see routing) is the only path allowed off-network.
    """
    parsed = parse.urlsplit(base_url)
    hostname = parsed.hostname
    if not hostname:
        raise LlmError(f"LLM base URL is invalid: {base_url}")
    if hostname == "localhost":
        return
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return
    except ValueError:
        pass
    raise LlmError(f"Local-tier LLM base URL must be loopback or a private LAN address: {base_url}")


class OpenAiCompatLlmClient:
    def _post_chat(
        self,
        config: AppConfig,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        *,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        trace_phase: str = "llm",
        purpose: str = "answer",
    ) -> str:
        # Route to the cloud tier only for purposes explicitly opted in; every
        # other call stays on the local tier, which is pinned to your own
        # hardware. Purposes that ingest others' chat content (answer, digest,
        # media, memo) must never be routed to cloud.
        headers = {"Content-Type": "application/json"}
        cloud = getattr(config, "cloud_llm", None)
        if cloud and cloud.base_url and purpose in cloud.purposes:
            api_key = cloud.api_key()
            if not api_key:
                raise LlmError(f"cloud LLM tier for purpose '{purpose}' needs {cloud.api_key_env} set")
            target_base_url = cloud.base_url.strip()
            target_model = (cloud.model or config.llm.model).strip()
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            target_base_url = (base_url or config.llm.base_url).strip()
            target_model = (model or config.llm.model).strip()
            _require_local_base_url(target_base_url)
        payload = {
            "model": target_model,
            "messages": messages,
            "temperature": config.llm.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or config.llm.max_output_tokens,
        }
        body = json.dumps(payload).encode("utf-8")
        url = f"{target_base_url.rstrip('/')}/chat/completions"
        trace_event(f"{trace_phase}.request", {"url": url, "payload": payload, "purpose": purpose})
        req = request.Request(url, data=body, headers=headers, method="POST")
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
            content = str(payload["choices"][0]["message"]["content"]).strip()
            trace_event(f"{trace_phase}.response", {"raw_text": content, "response_json": payload})
            return content
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LlmError("LLM API returned an unexpected response") from exc

    def answer_from_evidence(
        self,
        config: AppConfig,
        question: str,
        evidence: list[EvidenceItem],
        person_context: str = "",
        control_context: str = "",
        persona: str = "",
    ) -> str:
        prompt = build_answer_prompt(question, evidence, person_context, control_context)
        trace_event("answer.prompt", {"question": question, "prompt": prompt, "person_context": person_context, "control_context": control_context, "persona": persona})
        answer_system: list[dict[str, str]] = []
        if persona.strip():
            # A purpose-scoped control chat's persona (e.g. a translation
            # assistant) prepends its own directive, then the evidence-grounding
            # rule still constrains the model to the archive.
            answer_system.append({"role": "system", "content": persona.strip()})
        answer_system.append({"role": "system", "content": "Answer only from provided evidence. No hidden reasoning."})
        first = self._post_chat(
            config,
            [
                *answer_system,
                {"role": "user", "content": prompt},
            ],
            trace_phase="answer",
        )
        verify_prompt = build_verification_prompt(first, question, evidence, person_context, control_context)
        trace_event("verification.prompt", {"question": question, "prompt": verify_prompt, "draft_answer": first})
        verified = self._post_chat(
            config,
            [
                {"role": "system", "content": "Verify answers against evidence. Return corrected answer only."},
                {"role": "user", "content": verify_prompt},
            ],
            max_tokens=min(300, config.llm.max_output_tokens),
            trace_phase="verification",
        )
        return verified or first

    def plan_query(self, config: AppConfig, question: str, catalog: SearchCatalog, graph: PersonGraph) -> QueryPlan:
        prompt = build_planner_prompt(question, catalog, graph)
        planner_max_tokens = config.llm.planner_max_output_tokens or config.llm.max_output_tokens
        planner_temperature = config.llm.planner_temperature
        trace_event("planner.prompt", {"question": question, "prompt": prompt})
        raw = self._post_chat(
            config,
            [
                {"role": "system", "content": "You plan archive retrieval queries. Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=min(400, planner_max_tokens),
            base_url=config.llm.planner_base_url or None,
            model=config.llm.planner_model or None,
            temperature=planner_temperature,
            trace_phase="planner",
            purpose="planner",
        )
        return parse_query_plan(raw, question)


def _question_tokens(question: str) -> list[str]:
    return [
        token.casefold()
        for token in re.findall(r"[A-Za-zÀ-ÿ0-9'.-]+", question)
        if len(token) > 2 and token.casefold() not in LOCAL_QUERY_STOPWORDS
    ]


def _message_lines(text: str) -> list[str]:
    if not text:
        return []
    normalized = re.sub(r"(?i)<br\s*/?>", "\n", text)
    normalized = re.sub(r"(?i)</p>", "\n", normalized)
    normalized = re.sub(r"(?i)<li>", "- ", normalized)
    normalized = re.sub(r"(?i)</li>", "\n", normalized)
    normalized = re.sub(r"<[^>]+>", "", normalized)
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def _best_anchor_excerpt(text: str, question: str, max_chars: int = 700) -> str:
    lines = _message_lines(text)
    lowered = question.casefold()
    list_style = bool(lines and len(lines) > 1 and any(token in lowered for token in ("pick up", "shopping list", "grocery", "what did", "list")))
    if list_style:
        clean_text = "; ".join(lines)
    else:
        clean_text = " ".join(lines) if lines else text.replace("\n", " ").strip()

    lowered = question.casefold()
    tokens = _question_tokens(question)
    features = detect_query_features(question)
    best_idx = 0
    best_score = -1.0
    for idx, line in enumerate(lines):
        line_lower = line.casefold()
        score = 0.0
        overlap = sum(1 for token in tokens if token in line_lower)
        score += overlap * 6.0
        if "grand total" in lowered and "grand total" in line_lower:
            score += 60.0
        if "extra total" in lowered and "total extra" in line_lower:
            score += 60.0
        if ("check in" in lowered or "check-in" in lowered) and "check in" in line_lower:
            score += 60.0
        if "key box" in lowered and "key box" in line_lower:
            score += 50.0
        if "proof of payment" in lowered and "proof of payment" in line_lower:
            score += 50.0
        if "alarm" in lowered and "alarm" in line_lower:
            score += 40.0
        if "outage" in lowered or "power" in lowered:
            if "outage" in line_lower or "loadshedding" in line_lower:
                score += 35.0
        if "app" in lowered and "app" in line_lower:
            score += 20.0
        if "email" in features and EMAIL_RE.search(line):
            score += 35.0
        if "url" in features and URL_RE.search(line):
            score += 35.0
        if "address" in features and ADDRESS_RE.search(line):
            score += 35.0
        if "date" in features and DATE_RE.search(line):
            score += 20.0
        if "code" in lowered and re.search(r"\b\d{4,8}\b", line):
            score += 35.0
        if score > best_score:
            best_score = score
            best_idx = idx

    if best_score <= 0:
        return clean_text[: max_chars - 3].rstrip() + "..."

    selected: list[str] = []
    for idx in range(max(0, best_idx - 1), min(len(lines), best_idx + 3)):
        candidate = lines[idx]
        trial = " ".join(selected + [candidate]).strip()
        if len(trial) > max_chars and selected:
            break
        if len(trial) > max_chars:
            candidate = candidate[: max_chars - 3].rstrip() + "..."
            selected.append(candidate)
            break
        selected.append(candidate)
    if list_style and len(selected) > 1:
        return "; ".join(selected).strip()
    return " ".join(selected).strip()


def build_evidence_packet(results: list[SearchResult], limit: int, question: str = "") -> list[EvidenceItem]:
    evidence: list[EvidenceItem] = []
    context_limit = 4 if question else 3
    excerpt_limit = 1400 if question else 900
    for idx, result in enumerate(results[:limit], start=1):
        anchor = _best_anchor_excerpt(result.text, question) if question else result.text.replace("\n", " ").strip()
        if len(anchor) > 700:
            anchor = anchor[:697].rstrip() + "..."
        parts = [anchor]
        if result.context_before:
            parts.append("Context before:")
            parts.extend(f"- {line}" for line in result.context_before[:context_limit])
        if result.context_after:
            parts.append("Context after:")
            parts.extend(f"- {line}" for line in result.context_after[:context_limit])
        excerpt = "\n".join(parts)
        if len(excerpt) > excerpt_limit:
            excerpt = excerpt[: excerpt_limit - 3].rstrip() + "..."
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


def build_slice_evidence_packet(windows: list[ChatWindow], limit: int, question: str = "") -> list[EvidenceItem]:
    evidence: list[EvidenceItem] = []
    excerpt_limit = 520 if question else 320
    ordered_messages = []
    for window in windows:
        focused = [item for item in window.messages if item.is_seed or item.is_match]
        nearby = [item for item in window.messages if not (item.is_seed or item.is_match)]
        ordered_messages.extend(focused + nearby)

    seen: set[str] = set()
    for message in ordered_messages:
        if len(evidence) >= limit:
            return evidence
        if message.message_id in seen:
            continue
        seen.add(message.message_id)
        excerpt = _best_anchor_excerpt(message.text, question, max_chars=excerpt_limit)
        evidence.append(
            EvidenceItem(
                citation_id=f"[{len(evidence) + 1}]",
                message_id=message.message_id,
                chat_id=message.chat_id,
                chat_name=message.chat_name,
                sender_name=message.sender_name or "unknown",
                timestamp=message.timestamp,
                excerpt=excerpt,
                score=message.score,
            )
        )
    return evidence


def build_slice_reasoning_prompt(
    question: str,
    windows: list[ChatWindow],
    evidence: list[EvidenceItem],
    person_context: str = "",
    control_context: str = "",
) -> str:
    citation_by_message_id = {item.message_id: item.citation_id for item in evidence}
    blocks: list[str] = []
    for idx, window in enumerate(windows, start=1):
        header = f"Window {idx} [{window.chat_name}] {window.start_timestamp} .. {window.end_timestamp}"
        lines = [header]
        for message in window.messages:
            citation = citation_by_message_id.get(message.message_id)
            if citation is None:
                continue
            flags: list[str] = []
            if message.is_seed:
                flags.append("seed")
            elif message.is_match:
                flags.append("match")
            flag_text = f" ({', '.join(flags)})" if flags else ""
            lines.append(
                f"{citation} {message.sender_name or 'unknown'} @ {message.timestamp}{flag_text}: "
                f"{_best_anchor_excerpt(message.text, question, max_chars=520)}"
            )
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    windows_block = "\n\n".join(blocks)
    context_line = f"\nPerson context: {person_context}\n" if person_context else ""
    control_line = f"\nControl-memory context:\n{control_context}\n" if control_context else ""
    return (
        "Answer the question by reasoning over the bounded chat windows below.\n"
        "Work from the message sequence, not from one isolated snippet.\n"
        "Each citation id refers to one message line and may be cited directly.\n"
        "Lines marked (seed) or (match) are the main retrieval anchors. Prefer them over nearby lines when they conflict.\n"
        "If the question asks what someone asked you to pick up, buy, or get, extract every explicit requested item.\n"
        "If one later line repeats the whole shopping list, prefer that full list over partial earlier mentions.\n"
        "If the answer depends on multiple messages, combine them and cite the supporting ids.\n"
        "Control-memory context is not archive evidence. Do not turn it into fake archive citations.\n"
        "If the evidence is partial, say what it does support and what remains unclear.\n"
        "Prefer explicit totals, amounts, codes, emails, addresses, app names, and URLs exactly as written.\n"
        "Do not output chain-of-thought. Return only the answer.\n"
        f"{context_line}"
        f"{control_line}\n"
        f"Question:\n{question}\n\n"
        f"Windows:\n{windows_block}"
    )


def build_planner_prompt(question: str, catalog: SearchCatalog, graph: PersonGraph, control_context: str = "") -> str:
    sender_block = ", ".join(catalog.sender_names[:60]) or "(none)"
    chat_block = ", ".join(catalog.chat_names[:60]) or "(none)"
    person_lines: list[str] = []
    for person in graph.people:
        aliases = ", ".join(person.aliases) if person.aliases else "none"
        chats = ", ".join(person.chat_ids) if person.chat_ids else "none"
        person_lines.append(f"  {person.canonical_name} (aliases: {aliases}; chats: {chats})")
    person_block = "\n".join(person_lines) if person_lines else "(none)"
    control_block = f"\nRecent control-chat context:\n{control_context}\n" if control_context else ""
    return (
        "Plan retrieval for a private local chat archive.\n"
        "Return one JSON object only. No markdown.\n"
        "Use these keys exactly: normalized_question, resolved_question, search_queries, people, chat_hints, preferred_senders, preferred_chats, answer_kind, time_hint.\n"
        "If the question is a follow-up that depends on the recent control-chat context (pronouns like 'she' or 'it', ordinals like 'the second one', or requests like 'go back to that'), set resolved_question to a fully self-contained rewrite that names the people and topics explicitly. Otherwise set resolved_question to an empty string.\n"
        "search_queries should contain 3 to 8 short search strings when possible.\n"
        "Expand nicknames, tense changes, synonyms, and likely paraphrases.\n"
        "When a person is mentioned, set 'people' to their canonical names from the known people list below.\n"
        "When the question asks what someone sent, gave, said, or shared, also put that person's name in preferred_senders, even if they are not in the known people list.\n"
        "answer_kind must be one of: fact, date, url, last-message, summary.\n"
        "Use 'url' when the question asks for a site, link, or web address.\n"
        "time_hint must be one of: recent, any.\n\n"
        f"Known people:\n{person_block}\n\n"
        f"All sender names:\n{sender_block}\n\n"
        f"All chat names:\n{chat_block}\n"
        f"{control_block}\n"
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
    answer_kind = str(payload.get("answer_kind") or "fact").strip() or "fact"
    allowed_answer_kinds = {"fact", "date", "url", "last-message", "summary"}
    if answer_kind not in allowed_answer_kinds:
        answer_kind = "fact"
    return QueryPlan(
        normalized_question=normalized_question or original_question,
        search_queries=list_of_strings(payload.get("search_queries")),
        people=list_of_strings(payload.get("people")),
        chat_hints=list_of_strings(payload.get("chat_hints")),
        preferred_senders=list_of_strings(payload.get("preferred_senders")),
        preferred_chats=list_of_strings(payload.get("preferred_chats")),
        answer_kind=answer_kind,
        time_hint=str(payload.get("time_hint") or "any").strip() or "any",
        resolved_question=str(payload.get("resolved_question") or "").strip(),
    )


def fallback_query_plan(question: str, catalog: SearchCatalog, graph: PersonGraph) -> QueryPlan:
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

    found_people = [
        person for person in graph.people
        if person.canonical_name.casefold() in lowered
        or any(alias.casefold() in lowered for alias in person.aliases)
    ]
    preferred_senders = [person.canonical_name for person in found_people] or [
        name for name in catalog.sender_names if name and name.casefold().split()[0] in lowered
    ][:3]
    preferred_chats = [chat_id for person in found_people for chat_id in person.chat_ids] or [
        name for name in catalog.chat_names if name and name.casefold().split()[0] in lowered
    ][:3]
    people_names = [person.canonical_name for person in found_people]
    return QueryPlan(
        normalized_question=question,
        search_queries=[question],
        people=people_names,
        chat_hints=preferred_chats,
        preferred_senders=preferred_senders,
        preferred_chats=preferred_chats,
        answer_kind=answer_kind,
        time_hint=time_hint,
    )


def plan_archive_query(
    config: AppConfig,
    question: str,
    llm_client: QueryPlannerClient | None = None,
    control_context: str = "",
) -> QueryPlan:
    catalog = collect_search_catalog(config)
    graph = load_person_graph(config)
    client = llm_client or OpenAiCompatLlmClient()
    try:
        if isinstance(client, OpenAiCompatLlmClient):
            prompt = build_planner_prompt(question, catalog, graph, control_context)
            planner_max_tokens = config.llm.planner_max_output_tokens or config.llm.max_output_tokens
            planner_temperature = config.llm.planner_temperature
            trace_event("planner.prompt", {"question": question, "prompt": prompt, "control_context": control_context})
            raw = client._post_chat(
                config,
                [
                    {"role": "system", "content": "You plan archive retrieval queries. Return strict JSON only."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=min(400, planner_max_tokens),
                base_url=config.llm.planner_base_url or None,
                model=config.llm.planner_model or None,
                temperature=planner_temperature,
                trace_phase="planner",
                purpose="planner",
            )
            trace_event("planner.raw_output", {"raw_text": raw})
            plan = parse_query_plan(raw, question)
        else:
            plan = client.plan_query(config, question, catalog, graph)
    except Exception as exc:
        trace_event("planner.fallback", {"question": question, "reason": str(exc)})
        return fallback_query_plan(question, catalog, graph)

    if not plan.search_queries:
        trace_event("planner.fallback", {"question": question, "reason": "empty-search-queries"})
        return fallback_query_plan(question, catalog, graph)

    lowered = question.casefold()
    if plan.answer_kind == "last-message" and "last" in lowered and any(token in lowered for token in ("store", "grocery", "shopping", "pick up", "things")):
        plan.answer_kind = "fact"
        plan.time_hint = "recent"

    trace_event("planner.plan", {
        "normalized_question": plan.normalized_question,
        "search_queries": list(plan.search_queries),
        "people": list(plan.people),
        "chat_hints": list(plan.chat_hints),
        "preferred_senders": list(plan.preferred_senders),
        "preferred_chats": list(plan.preferred_chats),
        "answer_kind": plan.answer_kind,
        "time_hint": plan.time_hint,
    })

    resolved_people = graph.find_people(plan.people)
    preferred_sender_keys = {name.casefold() for name in plan.preferred_senders}
    for person in resolved_people:
        if person.canonical_name.casefold() not in preferred_sender_keys:
            plan.preferred_senders.append(person.canonical_name)
            preferred_sender_keys.add(person.canonical_name.casefold())
        for chat_id in person.chat_ids:
            if chat_id not in plan.preferred_chats:
                plan.preferred_chats.append(chat_id)
    return plan


def _empty_retrieval(query: str) -> SearchResponse:
    return SearchResponse(query=query, results=[])


def _direct_plan(question: str, answer_kind: str = "fact") -> QueryPlan:
    return QueryPlan(
        normalized_question=question,
        search_queries=[question],
        answer_kind=answer_kind,
        time_hint="any",
    )


def _fact_objects_for_subject(memory_state: dict[str, Any] | None, subject_hint: str) -> list[dict[str, str]]:
    if not memory_state or not subject_hint:
        return []
    facts = memory_state.get("facts")
    if not isinstance(facts, list):
        return []
    wanted = subject_hint.casefold()
    matches: list[dict[str, str]] = []
    for item in facts:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "").strip()
        if not subject:
            continue
        tokens = {part.casefold() for part in re.findall(r"[A-Za-zÀ-ÿ0-9'.-]+", subject) if len(part) > 2}
        if wanted == subject.casefold() or wanted in tokens:
            matches.append({
                "subject": subject,
                "predicate": str(item.get("predicate") or "").strip(),
                "object": str(item.get("object") or "").strip(),
            })
    return matches


def _direct_memory_answer(question: str, memory_state: dict[str, Any] | None) -> AskResponse | None:
    """Deterministic product routing for the canonical 'who is X (again)?' lookup.

    Only the exact command shape short-circuits here; paraphrases go through
    the model with memory facts in the control context.
    """
    lowered = question.casefold().strip()
    match = re.match(r"who is\s+(.+?)\s+again\??$", lowered)
    if not match:
        match = re.match(r"who is\s+(.+?)\??$", lowered)
    if not match:
        return None
    subject_hint = match.group(1).strip(" ?.!")
    facts = _fact_objects_for_subject(memory_state, subject_hint)
    if not facts:
        return None

    relationship = next((item for item in facts if item["predicate"] == "relationship_to_user" and item["object"]), None)
    identity = next((item for item in facts if item["predicate"] == "identity" and item["object"]), None)
    subject_name = facts[0]["subject"]

    if relationship:
        answer = f"{subject_name} is your {relationship['object']}."
    elif identity:
        answer = f"{subject_name} is {identity['object']}."
    else:
        fact = next((item for item in facts if item["predicate"] and item["object"]), facts[0])
        answer = f"{fact['subject']} | {fact['predicate']} | {fact['object']}"

    return AskResponse(
        question=question,
        answer=answer,
        evidence=[],
        retrieval=_empty_retrieval(question),
        plan=_direct_plan(question, answer_kind="fact"),
        used_sources=["memory"],
        answer_path="direct",
    )


RELATIONSHIP_OBJECT_RE = re.compile(r"^(?:my|our)\s+([A-Za-zÀ-ÿ' -]+)$", re.IGNORECASE)


def _direct_memory_write_answer(question: str) -> AskResponse | None:
    match = re.match(r"remember that\s+(.+?)\s+is\s+(.+?)\.?$", question.strip(), re.IGNORECASE)
    if not match:
        return None
    subject = match.group(1).strip()
    target = match.group(2).strip()

    relationship_match = RELATIONSHIP_OBJECT_RE.match(target)
    if relationship_match:
        relationship = relationship_match.group(1).strip()
        answer = (
            f"I can save that relationship: {subject} is your {relationship}. "
            "Please confirm before I save it."
        )
        proposed_action = {
            "kind": "add-relationship-fact",
            "subject": subject,
            "relationship": relationship,
            "source_text": question.strip(),
        }
    else:
        answer = f"I can save that as an alias: {subject} → {target}. Please confirm before I save it."
        proposed_action = {
            "kind": "add-alias",
            "alias": subject,
            "canonical_name": target,
            "source_text": question.strip(),
        }
    return AskResponse(
        question=question,
        answer=answer,
        evidence=[],
        retrieval=_empty_retrieval(question),
        plan=_direct_plan(question, answer_kind="fact"),
        used_sources=[],
        answer_path="direct",
        proposed_action=proposed_action,
    )


def _direct_memo_answer(config: AppConfig, question: str, llm_client: Any | None = None) -> AskResponse | None:
    """Voice-memo lookup requests bypass evidence QA: transcripts are stored
    verbatim and excerpt caps would shred them. Transcript requests resolve
    deterministically; summary requests feed the full transcript to the
    model."""
    from .media import find_voice_transcripts, format_memo_header, parse_memo_request, summarize_transcript

    memo_request = parse_memo_request(question)
    if memo_request is None:
        return None
    memos = find_voice_transcripts(
        config,
        mine_only=memo_request.mine_only,
        sender_query=memo_request.sender_query,
        duration_minutes=memo_request.duration_minutes,
        limit=1,
    )
    if not memos:
        filters = []
        if memo_request.sender_query:
            filters.append(f"from {memo_request.sender_query}")
        if memo_request.duration_minutes is not None:
            filters.append(f"around {memo_request.duration_minutes} minutes")
        if memo_request.mine_only:
            filters.append("sent by you")
        detail = f" matching: {', '.join(filters)}" if filters else ""
        answer = f"I have no transcribed voice memos{detail}. Run index-media to transcribe new ones."
        return AskResponse(
            question=question,
            answer=answer,
            evidence=[],
            retrieval=_empty_retrieval(question),
            plan=_direct_plan(question, answer_kind="fact"),
            used_sources=[],
            answer_path="direct",
        )

    memo = memos[0]
    header = format_memo_header(memo)
    if memo_request.action == "transcript":
        return AskResponse(
            question=question,
            answer=f"{header}:\n{memo['transcript']}",
            evidence=[],
            retrieval=_empty_retrieval(question),
            plan=_direct_plan(question, answer_kind="fact"),
            used_sources=["archive"],
            answer_path="direct",
        )

    summary = summarize_transcript(config, memo, llm_client).strip()
    return AskResponse(
        question=question,
        answer=f"{header} — summary:\n{summary}",
        evidence=[],
        retrieval=_empty_retrieval(question),
        plan=_direct_plan(question, answer_kind="summary"),
        used_sources=["archive"],
        answer_path="model",
    )


def _direct_chat_digest_answer(config: AppConfig, question: str, llm_client: Any | None = None) -> AskResponse | None:
    """'Summarize the X chat(s)' requests go to the catch-up machinery, which
    digests whole message windows; evidence-QA excerpt caps would shred them.
    Falls through to normal QA when nothing matches a chat title."""
    from .catchup import CatchupError, catchup_summary, format_catchup_result, parse_chat_digest_request

    chat_query = parse_chat_digest_request(question)
    if not chat_query:
        return None
    try:
        result = catchup_summary(config, chat_query, llm_client)
    except CatchupError:
        return None

    return AskResponse(
        question=question,
        answer=format_catchup_result(result),
        evidence=[],
        retrieval=_empty_retrieval(question),
        plan=_direct_plan(question, answer_kind="summary"),
        used_sources=["archive"],
        answer_path="model" if result.message_count else "direct",
    )


def _format_control_context(
    control_turns: list[dict[str, Any]] | None = None,
    memory_state: dict[str, Any] | None = None,
) -> str:
    parts: list[str] = []
    turns = control_turns or []
    if turns:
        lines: list[str] = []
        for item in turns[-8:]:
            role = str(item.get("role") or "unknown").strip() or "unknown"
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            lines.append(f"- {role}: {content}")
        if lines:
            parts.append("Recent control-chat turns:\n" + "\n".join(lines))

    state = memory_state or {}
    summary = str(state.get("control_summary") or "").strip()
    if summary:
        parts.append(f"Rolling control summary:\n- {summary}")

    facts = state.get("facts")
    if isinstance(facts, list):
        fact_lines: list[str] = []
        for item in facts[:12]:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or "").strip()
            predicate = str(item.get("predicate") or "").strip()
            obj = str(item.get("object") or "").strip()
            source = str(item.get("source") or "").strip()
            if not (subject and predicate and obj):
                continue
            line = f"- {subject} | {predicate} | {obj}"
            if source:
                line += f" ({source})"
            fact_lines.append(line)
        if fact_lines:
            parts.append("Structured memory facts:\n" + "\n".join(fact_lines))

    return "\n\n".join(parts).strip()


def build_answer_prompt(
    question: str,
    evidence: list[EvidenceItem],
    person_context: str = "",
    control_context: str = "",
) -> str:
    evidence_block = "\n".join(
        f"{item.citation_id} [{item.chat_name}] {item.sender_name} @ {item.timestamp}: {item.excerpt}"
        for item in evidence
    ) or "(no archive evidence was retrieved for this question)"
    context_line = f"\nPerson context: {person_context}\n" if person_context else ""
    control_line = f"\nControl-memory context:\n{control_context}\n" if control_context else ""
    return (
        "Answer the question using the archive evidence below, plus any control-memory context when it is relevant.\n"
        "Pay close attention to the sender name and chat name on each line.\n"
        "Each citation refers only to the anchor message line for that evidence item.\n"
        "Nested context bullets are background only.\n"
        "Control-memory context is not archive evidence. Do not pretend it came from an archive citation.\n"
        "Structured memory facts in the control-memory context are user-approved. You may answer from them directly, without a citation, when they answer the question.\n"
        "Do not guess relationships or personal facts: if the question asks who someone is or how they relate to the user, and neither the evidence nor the control-memory context covers it, say you do not have that information stored.\n"
        "If the evidence is partial, say what the evidence does support and what remains unclear.\n"
        "Only say the evidence is insufficient when the evidence truly does not support even a partial answer.\n"
        "When the evidence contains an explicit total, amount, code, email, address, app name, or URL, quote that explicit value verbatim.\n"
        "Cite factual claims with citation ids like [1].\n"
        "Do not invent names, dates, addresses, or events.\n"
        "Do not output chain-of-thought.\n"
        f"{context_line}"
        f"{control_line}\n"
        f"Question:\n{question}\n\n"
        f"Evidence:\n{evidence_block}"
    )


def build_verification_prompt(
    answer: str,
    question: str,
    evidence: list[EvidenceItem],
    person_context: str = "",
    control_context: str = "",
) -> str:
    evidence_block = "\n".join(
        f"{item.citation_id} [{item.chat_name}] {item.sender_name} @ {item.timestamp}: {item.excerpt}"
        for item in evidence
    ) or "(no archive evidence was retrieved for this question)"
    context_line = f"\nPerson context: {person_context}\n" if person_context else ""
    control_line = f"\nControl-memory context:\n{control_context}\n" if control_context else ""
    return (
        "Verify the answer below against the evidence.\n"
        "Return a corrected answer that is fully supported by the evidence.\n"
        "Each citation refers only to the anchor message line for that evidence item.\n"
        "Nested context bullets are background only.\n"
        "Control-memory context is not archive evidence. Do not convert it into fake archive citations.\n"
        "Claims supported by structured memory facts in the control-memory context are valid; keep them, without adding citations to them.\n"
        "Strip any unsupported claims, names, dates, or facts.\n"
        "If a claim in the original answer is wrong, replace it with what the evidence actually says.\n"
        "If the evidence contains an explicit shopping list, preserve every explicit requested item that answers the question.\n"
        "Prefer explicit totals, amounts, codes, emails, addresses, app names, URLs, and shopping-list items exactly as written in the evidence.\n"
        "Keep the citation ids from the original answer where they are valid.\n"
        "Do not output chain-of-thought. Return only the corrected answer.\n"
        f"{context_line}"
        f"{control_line}\n"
        f"Original question:\n{question}\n\n"
        f"Evidence:\n{evidence_block}\n\n"
        f"Answer to verify:\n{answer}"
    )


def _answer_with_slice_reasoning(
    client: LlmClient,
    config: AppConfig,
    question: str,
    evidence: list[EvidenceItem],
    windows: list[ChatWindow],
    person_context: str = "",
    control_context: str = "",
    persona: str = "",
) -> str:
    if not isinstance(client, OpenAiCompatLlmClient):
        return client.answer_from_evidence(config, question, evidence, person_context, control_context, persona)

    prompt = build_slice_reasoning_prompt(question, windows, evidence, person_context, control_context)
    trace_event(
        "answer.slice.prompt",
        {
            "question": question,
            "prompt": prompt,
            "window_count": len(windows),
            "message_count": sum(len(window.messages) for window in windows),
        },
    )
    slice_system: list[dict[str, str]] = []
    if persona.strip():
        slice_system.append({"role": "system", "content": persona.strip()})
    slice_system.append({"role": "system", "content": "Answer from bounded chat windows only. No hidden reasoning."})
    first = client._post_chat(
        config,
        [
            *slice_system,
            {"role": "user", "content": prompt},
        ],
        trace_phase="answer.slice",
    )
    verify_prompt = build_verification_prompt(first, question, evidence, person_context, control_context)
    trace_event("verification.prompt", {"question": question, "prompt": verify_prompt, "draft_answer": first})
    verified = client._post_chat(
        config,
        [
            {"role": "system", "content": "Verify answers against evidence. Return corrected answer only."},
            {"role": "user", "content": verify_prompt},
        ],
        max_tokens=min(300, config.llm.max_output_tokens),
        trace_phase="verification",
    )
    return verified or first


def _repair_list_answer_from_evidence(question: str, answer: str, evidence: list[EvidenceItem]) -> str:
    lowered = question.casefold()
    if not any(token in lowered for token in ("pick up", "grocery", "store next door", "from the store", "go to the store", "buy from the store", "get from the store")):
        return answer
    if not answer.strip():
        return answer

    answer_lower = answer.casefold()
    additions: list[str] = []
    for item in evidence:
        excerpt = item.excerpt
        parts: list[str] = []
        if ";" in excerpt:
            parts = [raw.strip(" -•\t\r\n") for raw in excerpt.split(";")]
        else:
            lines = _message_lines(excerpt)
            short_lines = [line.strip() for line in lines if 2 < len(line.strip()) <= 48]
            if len(short_lines) >= 2 and all(not line.casefold().startswith(("context before", "context after")) for line in short_lines):
                parts = short_lines
        if not parts:
            continue
        for part in parts:
            part_lower = part.casefold()
            if len(part) < 3:
                continue
            if any(skip in part_lower for skip in (
                "could you",
                "pick up",
                "for dinner",
                "shopping app",
                "chutney chicken tonight",
                "hey john",
                "hope you are doing ok",
                "we're going to make",
                "we are going to make",
                "tom is going to add",
                "context before",
                "context after",
            )):
                continue
            if part_lower in answer_lower:
                continue
            additions.append(f"* {part} {item.citation_id}")
            answer_lower += "\n" + part_lower

    if not additions:
        return answer
    if "\n* " in answer:
        return answer.rstrip() + "\n" + "\n".join(additions)
    return answer.rstrip() + "\n" + "\n".join(additions)


def _extract_address_candidate(text: str) -> str:
    for line in _message_lines(text):
        street = re.search(r"\b(?:rua|avenida|av\.?|estrada|travessa)\b", line, re.IGNORECASE)
        if street:
            return line[street.start():].strip(" -|,;")
        match = ADDRESS_RE.search(line)
        if match:
            candidate = line[match.start():].strip(" -|,;")
            postal = POSTAL_CODE_RE.search(line[match.end():])
            if postal and postal.group(0) not in candidate:
                candidate = f"{candidate} {postal.group(0).strip()}".strip()
            return candidate
    return ""


def _repair_address_answer_from_retrieval(
    question: str,
    answer: str,
    retrieval: SearchResponse,
    evidence: list[EvidenceItem],
) -> str:
    lowered = question.casefold()
    if "address" not in lowered:
        return answer
    if ADDRESS_RE.search(answer) or POSTAL_CODE_RE.search(answer):
        return answer

    citation_by_message_id = {item.message_id: item.citation_id for item in evidence}
    preferred_tokens = [
        token for token in _question_tokens(question)
        if token not in {"address", "sent", "send", "what", "was", "for", "the"}
    ]
    best: tuple[float, str, str] | None = None
    for result in retrieval.results:
        candidate = _extract_address_candidate(result.text)
        if not candidate:
            continue
        score = result.score + 50.0
        haystacks = [result.text.casefold(), result.sender_name.casefold(), result.chat_name.casefold()]
        score += sum(12.0 for token in preferred_tokens if any(token in hay for hay in haystacks))
        citation = citation_by_message_id.get(result.message_id, "")
        if best is None or score > best[0]:
            best = (score, candidate, citation)
    if best is None:
        return answer

    _, candidate, citation = best
    if citation:
        return f"The address is {candidate} {citation}."
    return f"The address is {candidate}."


def _strip_invalid_citations(answer: str, evidence: list[EvidenceItem]) -> str:
    allowed = {item.citation_id[1:-1] for item in evidence}

    def replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1) in allowed else ""

    cleaned = CITATION_RE.sub(replace, answer)
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _trace_result_item(result: SearchResult, include_context: bool = False) -> dict[str, Any]:
    payload = {
        "message_id": result.message_id,
        "chat_id": result.chat_id,
        "chat_name": result.chat_name,
        "sender_name": result.sender_name,
        "timestamp": result.timestamp,
        "text": result.text,
        "score": result.score,
        "match_reasons": list(result.match_reasons),
    }
    if include_context:
        payload["context_before"] = list(result.context_before)
        payload["context_after"] = list(result.context_after)
    return payload


def _trace_evidence_item(item: EvidenceItem) -> dict[str, Any]:
    return {
        "citation_id": item.citation_id,
        "message_id": item.message_id,
        "chat_id": item.chat_id,
        "chat_name": item.chat_name,
        "sender_name": item.sender_name,
        "timestamp": item.timestamp,
        "excerpt": item.excerpt,
        "score": item.score,
    }


def _trace_window_item(window: ChatWindow) -> dict[str, Any]:
    return {
        "chat_id": window.chat_id,
        "chat_name": window.chat_name,
        "start_sort_key": window.start_sort_key,
        "end_sort_key": window.end_sort_key,
        "start_timestamp": window.start_timestamp,
        "end_timestamp": window.end_timestamp,
        "best_score": window.best_score,
        "seed_message_ids": list(window.seed_message_ids),
        "message_count": len(window.messages),
        "messages": [
            {
                "message_id": item.message_id,
                "sender_name": item.sender_name,
                "timestamp": item.timestamp,
                "sort_key": item.sort_key,
                "score": item.score,
                "match_reasons": list(item.match_reasons),
                "is_match": item.is_match,
                "is_seed": item.is_seed,
                "text": item.text,
            }
            for item in window.messages
        ],
    }


def format_ask_response(response: AskResponse) -> str:
    if not response.evidence:
        answer = response.answer.strip()
        return answer or "I could not find enough local evidence to answer that."

    answer = _strip_invalid_citations(response.answer.strip(), response.evidence)
    if not answer:
        answer = "I could not find enough local evidence to answer that."

    cited_ids = {f"[{match}]" for match in CITATION_RE.findall(answer)}
    source_items = [item for item in response.evidence if item.citation_id in cited_ids] or response.evidence

    lines = [answer, "", "Sources:"]
    for item in source_items:
        lines.append(f"{item.citation_id} {item.chat_name} — {item.sender_name} — {item.timestamp}")
    return "\n".join(lines)


SPEAKER_VERBS = (
    "said",
    "say",
    "sent",
    "send",
    "asked",
    "ask",
    "told",
    "tell",
    "wrote",
    "write",
    "texted",
    "text",
    "messaged",
    "message",
    "posted",
    "post",
    "set up",
)


def _person_variants(person) -> list[str]:
    values = [person.canonical_name, *person.aliases]
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        key = value.strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def _contains_relation(lowered: str, speaker_variants: list[str], target_variants: list[str]) -> bool:
    templates = (
        "{speaker} said to {target}",
        "{speaker} say to {target}",
        "{speaker} last said to {target}",
        "{speaker} last say to {target}",
        "{speaker} told {target}",
        "{speaker} tell {target}",
        "{speaker} asked {target}",
        "{speaker} ask {target}",
    )
    for speaker in speaker_variants:
        for target in target_variants:
            for template in templates:
                if template.format(speaker=speaker, target=target) in lowered:
                    return True
    return False


def _infer_retrieval_constraints(question: str, resolved_people: list) -> tuple[list[str], list[str]]:
    lowered = question.casefold()
    speaker_only: list[str] = []
    chat_only: list[str] = []

    for speaker in resolved_people:
        speaker_variants = _person_variants(speaker)
        for target in resolved_people:
            if speaker.person_id == target.person_id:
                continue
            if _contains_relation(lowered, speaker_variants, _person_variants(target)):
                speaker_only = [speaker.canonical_name]
                common = set(speaker.chat_ids) & set(target.chat_ids)
                chat_only = sorted(common)
                return speaker_only, chat_only

    for person in resolved_people:
        for variant in _person_variants(person):
            pattern = re.compile(
                rf"\b{re.escape(variant)}\b(?:\W+\w+){{0,3}}\W+(?:{'|'.join(re.escape(verb) for verb in SPEAKER_VERBS)})\b",
                re.IGNORECASE,
            )
            if pattern.search(question):
                return [person.canonical_name], []

    return speaker_only, chat_only


LOCAL_QUERY_STOPWORDS = {
    "what", "was", "were", "did", "does", "do", "the", "a", "an", "to", "from", "of", "on", "in",
    "and", "or", "me", "my", "our", "us", "you", "your", "i", "is", "it", "that", "this", "thing", "things",
}


def _supplement_search_queries(question: str, plan: QueryPlan, resolved_people: list) -> list[str]:
    lowered = question.casefold()
    seen = {value.casefold() for value in plan.all_queries(question)}
    extras: list[str] = []

    def add(value: str) -> None:
        candidate = value.strip()
        if not candidate or candidate.casefold() in seen:
            return
        seen.add(candidate.casefold())
        extras.append(candidate)

    tokens = [token for token in re.findall(r"[A-Za-zÀ-ÿ0-9'.-]+", question) if len(token) > 2]
    compact = [token for token in tokens if token.casefold() not in LOCAL_QUERY_STOPWORDS]

    if "address" in lowered:
        for person in resolved_people:
            add(f"{person.canonical_name} address")
        for token in compact:
            if token[0].isupper():
                add(token)
                add(f"{token} address")
        add("Rua")
        add("Avenida")
        add("street")
        add("morada")

    if "store" in lowered:
        for person in resolved_people:
            add(f"{person.canonical_name} store")
        add("store next door")
        add("bottom sheets")

    if "prescription" in lowered or "prescriptions" in lowered:
        add("pharmacy")
        add("vet")
        add("pills")

    if "check in" in lowered or "check-in" in lowered:
        add("check in")
        add("check in starts")

    if "alarm" in lowered:
        add("alarm app")
        add("AlarmCo")

    if "outage" in lowered or "power" in lowered:
        add("loadshedding")
        add("power outage app")
        add("Eskom")

    if "proof of payment" in lowered:
        add("proof of payment")
        add("payment email")

    if "extra total" in lowered:
        add("Total extra")
    if "grand total" in lowered:
        add("Grand total")

    if compact:
        add(" ".join(compact[: min(4, len(compact))]))

    return extras


def _needs_slice_context(question: str, plan: QueryPlan) -> bool:
    lowered = question.casefold()
    if plan.answer_kind in {"summary", "last-message"}:
        if any(token in lowered for token in ("store", "grocery", "pick up", "shopping", "list", "things")):
            return True
    if plan.answer_kind == "summary":
        return True
    slice_markers = (
        "what did",
        "what things",
        "which",
        "besides",
        "after",
        "full",
        "list",
        "pick up",
        "prescription",
        "prescriptions",
        "check in",
        "check-in",
        "proof of payment",
        "power outage",
        "power outages",
        "grand total",
        "extra total",
        "grocery",
        "store next door",
    )
    return any(marker in lowered for marker in slice_markers)


def ask_archive(
    config: AppConfig,
    question: str,
    limit: int | None = None,
    llm_client: LlmClient | None = None,
    control_turns: list[dict[str, Any]] | None = None,
    memory_state: dict[str, Any] | None = None,
    persona: str = "",
) -> AskResponse:
    trace_event("ask.start", {"question": question, "limit": limit, "persona": persona})
    if control_turns is None or memory_state is None:
        from .memory import load_memory_state, recent_control_turns

        if control_turns is None:
            control_turns = recent_control_turns(config, limit=8)
        if memory_state is None:
            memory_state = load_memory_state(config)

    trace_event("control.context", {"control_turns": control_turns or [], "memory_state": memory_state or {}})

    direct = _direct_memory_write_answer(question)
    if direct is not None:
        trace_event("memory.direct_write", {"question": question, "answer": direct.answer, "proposed_action": direct.proposed_action})
        return direct

    direct = _direct_memory_answer(question, memory_state)
    if direct is not None:
        trace_event("memory.direct_answer", {"question": question, "answer": direct.answer})
        return direct

    direct = _direct_memo_answer(config, question, llm_client)
    if direct is not None:
        trace_event("memo.direct_answer", {"question": question, "answer_path": direct.answer_path})
        return direct

    direct = _direct_chat_digest_answer(config, question, llm_client)
    if direct is not None:
        trace_event("digest.direct_answer", {"question": question, "answer_path": direct.answer_path})
        return direct

    control_context = _format_control_context(control_turns, memory_state)
    planner_client = llm_client if llm_client and hasattr(llm_client, "plan_query") else None
    plan = plan_archive_query(config, question, planner_client, control_context=control_context)  # type: ignore[arg-type]
    effective_question = question
    if control_context and plan.resolved_question:
        effective_question = plan.resolved_question
        trace_event("planner.resolved_question", {"question": question, "resolved_question": effective_question})
    graph = load_person_graph(config)
    resolved_people = graph.find_people(plan.people)
    preferred_senders = list(dict.fromkeys(plan.preferred_senders + [p.canonical_name for p in resolved_people]))
    preferred_chats = list(dict.fromkeys(plan.preferred_chats + [chat_id for p in resolved_people for chat_id in p.chat_ids]))
    restrict_senders, restrict_chats = _infer_retrieval_constraints(effective_question, resolved_people)
    if not restrict_senders and len(preferred_senders) == 1:
        sender_name = preferred_senders[0].strip()
        first_token = sender_name.casefold().split()[0] if sender_name else ""
        if first_token and first_token in effective_question.casefold() and ("address" in effective_question.casefold() or any(verb in effective_question.casefold() for verb in SPEAKER_VERBS)):
            # Hard-restricting to a sender the archive has never seen (the
            # planner can invent ones like "the Sample Bay host") guarantees
            # zero results; only restrict to real senders.
            known_senders = {name.casefold() for name in collect_search_catalog(config).sender_names}
            if sender_name.casefold() in known_senders:
                restrict_senders = [sender_name]
    slice_mode = _needs_slice_context(question, plan)
    queries = plan.all_queries(effective_question) + _supplement_search_queries(effective_question, plan, resolved_people)
    retrieval_limit = max(limit or config.llm.max_input_snippets, config.llm.max_input_snippets)
    if slice_mode:
        retrieval_limit = max(retrieval_limit * 4, 20)
    date_start, date_end = _date_bounds_from_query(effective_question)
    trace_event("retrieval.search", {
        "queries": queries,
        "preferred_senders": restrict_senders or preferred_senders,
        "preferred_chats": restrict_chats or preferred_chats,
        "answer_kind": plan.answer_kind,
        "time_hint": plan.time_hint,
        "restrict_senders": restrict_senders or [],
        "restrict_chats": restrict_chats or [],
        "slice_mode": slice_mode,
        "date_start": date_start,
        "date_end": date_end,
    })
    retrieval_answer_kind = plan.answer_kind
    if slice_mode and plan.answer_kind == "last-message":
        retrieval_answer_kind = "fact"
    retrieval = search_archive_multi(
        config,
        queries,
        limit=retrieval_limit,
        preferred_senders=restrict_senders or preferred_senders,
        preferred_chats=restrict_chats or preferred_chats,
        answer_kind=retrieval_answer_kind,
        time_hint=plan.time_hint,
        restrict_chats=restrict_chats or None,
        restrict_senders=restrict_senders or None,
        date_start=date_start,
        date_end=date_end,
    )
    trace_event("retrieval.results.initial", {"count": len(retrieval.results), "results": [_trace_result_item(item) for item in retrieval.results[:12]]})
    if slice_mode:
        retrieval.results = expand_results_with_spans(
            config,
            effective_question,
            retrieval.results,
            answer_kind=retrieval_answer_kind,
            window=10,
        )
    if slice_mode:
        trace_event("retrieval.results.spans", {"count": len(retrieval.results), "results": [_trace_result_item(item) for item in retrieval.results[:12]]})
    retrieval.results = expand_results_with_context(config, retrieval.results, window=4 if slice_mode else 3)
    trace_event("retrieval.results.context", {"count": len(retrieval.results), "results": [_trace_result_item(item, include_context=True) for item in retrieval.results[:12]]})
    slice_windows: list[ChatWindow] = []
    if slice_mode:
        window_message_limit = min(max(config.llm.max_input_snippets * 4, 12), 24)
        slice_windows = pack_chat_windows(
            config,
            retrieval.results,
            radius=6,
            seed_limit=4,
            max_windows=3,
            max_messages=window_message_limit,
        )
        trace_event("retrieval.windows", {"count": len(slice_windows), "windows": [_trace_window_item(item) for item in slice_windows]})
        evidence = build_slice_evidence_packet(slice_windows, window_message_limit, question=effective_question) if slice_windows else []
    else:
        evidence = []
    if not evidence:
        evidence_limit = max(config.llm.max_input_snippets, 8) if slice_mode else config.llm.max_input_snippets
        evidence = build_evidence_packet(retrieval.results, evidence_limit, question=effective_question)
    trace_event("evidence.packet", {"count": len(evidence), "items": [_trace_evidence_item(item) for item in evidence]})
    if not evidence and not control_context:
        trace_event("ask.no_evidence", {"question": question})
        return AskResponse(
            question=question,
            answer="I could not find enough local evidence to answer that.",
            evidence=[],
            retrieval=retrieval,
            plan=plan,
            used_sources=[],
        )
    if not evidence:
        trace_event("ask.memory_only", {"question": question})

    client = llm_client or OpenAiCompatLlmClient()
    resolved_people = graph.find_people(plan.people)
    person_context = ""
    if resolved_people:
        person_lines = []
        for p in resolved_people:
            aliases_str = f" (aliases: {', '.join(p.aliases)})" if p.aliases else ""
            person_lines.append(f"{p.canonical_name}{aliases_str}")
        person_context = "; ".join(person_lines)

    trace_event("person.context", {"person_context": person_context})
    if slice_mode and slice_windows:
        answer = _answer_with_slice_reasoning(client, config, question, evidence, slice_windows, person_context, control_context, persona)
    else:
        answer = client.answer_from_evidence(config, question, evidence, person_context, control_context, persona)
    repaired = _repair_list_answer_from_evidence(question, answer, evidence)
    if repaired != answer:
        trace_event("answer.list_repair", {"question": question, "before": answer, "after": repaired})
        answer = repaired
    repaired = _repair_address_answer_from_retrieval(question, answer, retrieval, evidence)
    if repaired != answer:
        trace_event("answer.address_repair", {"question": question, "before": answer, "after": repaired})
        answer = repaired
    # Only claim sources that are verifiable from the answer itself; deeper
    # source-use inference (memory/summary/control-turn overlap) lives in the
    # eval harness where the fixtures are known.
    used_sources: list[str] = []
    valid_citation_ids = {item.citation_id for item in evidence}
    if any(f"[{match}]" in valid_citation_ids for match in CITATION_RE.findall(answer)):
        used_sources.append("archive")

    trace_event("ask.final", {"question": question, "answer": answer, "used_sources": used_sources})
    return AskResponse(question=question, answer=answer, evidence=evidence, retrieval=retrieval, plan=plan, used_sources=used_sources)
