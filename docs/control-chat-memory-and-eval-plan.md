# Control-chat memory and eval plan

## 1. Goal

Extend the bot from archive QA into a control-chat assistant with bounded conversational memory, explicit user-approved facts, and stable regression tests for both.

The work must keep two properties:
- local-only storage and inference
- evidence-backed answers for archive facts

## 2. Product target

The control chat should support three kinds of memory:

1. recent turns
   - keep enough local turn history to resolve short follow-ups
   - examples: "that one", "what about her", "use the second answer"

2. rolling summaries
   - compress older control-chat turns into short state notes
   - preserve goals, open threads, pending corrections, and user preferences

3. structured memory
   - people
   - aliases
   - relationship facts
   - user-approved durable facts with provenance

The model should not be the only memory store.
Prompt context is a working set, not the system of record.

## 3. Work order

Do the work in this order:

1. finish the harness design
2. run a small headroom prescreen on candidate models
3. research and shortlist 3 to 5 models for the real comparison set
4. build the control-chat memory and facts substrate
5. add control-memory eval suites
6. run the real model shootout against the expanded harness

Reason:
- a full shootout now would overfit the old task
- a full memory layer now would land without a clean test contract
- a small prescreen now is still useful because headroom matters for the new design

## 4. Harness requirements

### 4.1 Core principle

The harness must test three things separately:
- archive retrieval and evidence QA
- bounded slice reasoning over retrieved spans
- control-chat continuity and memory use

Do not merge them into one score too early.
Keep per-suite scores and a small summary dashboard.

### 4.2 Run modes

The harness should support these run modes:
- smoke: starter-only gate
- broad: core plus slice
- control-memory: new control-chat suites
- full: all scored suites

### 4.3 Determinism

Add an eval-time deterministic mode.
At minimum:
- answer temperature override
- planner temperature override
- optional fixed output cap
- record model alias and runtime settings in output

This becomes the default for model comparison runs.

## 5. New eval classes

### 5.1 Control-chat continuity

Test short bounded follow-up behavior.

Examples:
- user asks a question, then asks "what about the second one"
- user corrects a name in the next turn
- user asks the bot to reuse its prior answer shape
- user asks a follow-up with pronouns only

Pass condition:
- the bot resolves the follow-up from the retained control-chat window
- the bot does not confuse control-chat context with archive evidence

### 5.2 Structured memory

Test use of durable stored facts.

Examples:
- Anna is my sister
- Tom is my brother in law
- Addy is my girlfriend
- Addy is an alias for Adrienne Peña

Pass condition:
- the bot uses stored facts when relevant
- the bot can combine stored facts with archive evidence
- the bot does not invent unstored relationships

### 5.3 Memory update actions

Test safe writes from the control chat.

Examples:
- add person
- add alias
- add relationship fact
- update existing alias set
- reject ambiguous writes until confirmed

Pass condition:
- the bot proposes or performs the right structured update
- persistent writes require explicit confirmation unless the command is already structured and unambiguous

### 5.4 Context-budget ladder

Test degradation as control-chat length grows.

Run the same intent over several context conditions:
- recent turns only
- recent turns plus rolling summary
- longer thread with summary refresh
- stress case near the configured budget

Record where failures begin.
The point is not perfect recall of arbitrary long threads. The point is stable bounded behavior.

### 5.5 Retrieval isolation

For mixed memory questions, verify source use.

Examples:
- "Who is Addy again, and what address did she send?"
- relationship should come from structured memory
- address should come from archive evidence

Pass condition:
- stored facts and archive citations are not conflated

## 6. Eval data model changes

Add optional fields to eval cases:
- `control_turns`: ordered prior control-chat turns
- `memory_state`: structured people, aliases, and facts loaded before the case
- `expected_actions`: expected memory writes or confirmations
- `expected_sources`: expected source classes such as `archive`, `memory`, `summary`
- `context_budget_class`: `short`, `medium`, `long`, `stress`
- `metrics_only`: allow non-gating stress cases

Keep existing archive QA cases valid.
New fields must be optional.

## 7. Runtime metrics to record

Per eval run, record:
- model alias
- `llama.cpp` version or commit if available
- context size
- temperature settings
- planner settings
- wall time per case
- first-token latency if available
- prompt tokens and output tokens if exposed
- VRAM used before and after the run, if obtainable
- pass/fail by suite

These do not all need gating at first.
But they must be logged so model tradeoffs are visible.

## 8. Control-chat memory substrate

### 8.1 New state classes

Add persistent state for:
- recent control turns
- rolling control summaries
- user facts
- fact provenance
- people and aliases, reusing the existing people graph where possible
- relationship facts between user and known people

### 8.2 Likely tables

The final schema may differ, but plan for records like:
- `control_turns`
- `control_summaries`
- `facts`
- `fact_sources`
- `fact_edges` or equivalent relationship rows
- `memory_update_queue` for pending confirmations

### 8.3 Update policy

Persistent memory writes should be safe by default.

Rules:
- direct structured commands may write immediately
- natural-language memory updates should require confirmation when ambiguous
- every durable fact should keep source text and write time
- facts should be editable and revocable

## 9. Model selection policy for this phase

Use a small prescreen before the full shootout.
A candidate model should survive all of these before it enters the main comparison set:
- loads cleanly at the target context size
- full or acceptable GPU offload on the 16 GB card
- passes a smoke question
- produces usable scores on `starter`
- shows enough headroom or quality to justify deeper testing

The main comparison set should then contain:
- the current Gemma baseline
- one or two headroom-friendly models
- one stronger stretch model only if runtime remains practical

## 10. Immediate next tasks

1. add deterministic eval mode
2. extend eval case schema for control-chat fixtures and memory fixtures
3. add empty suite files for control-memory and context-ladder runs
4. implement a small prescreen command or runbook for candidate models
5. research and shortlist 3 to 5 candidate models for the expanded harness

## 11. Candidate model directions

Prioritize models that either:
- may beat Gemma on archive and slice tasks, or
- come close while leaving more headroom for control-chat memory

Current likely directions:
- Mistral Nemo 12B Instruct
- Qwen3 14B
- one higher-fidelity 14B quant if it still fits comfortably
- one stretch candidate only if the runtime budget remains sane

Avoid spending early time on:
- coder models
- MoE curiosity runs
- barely fitting 30B+ models that force poor runtime settings

## 12. Definition of done for this phase

This phase is done when:
- the harness can score archive QA, slice reasoning, and control-memory tasks separately
- deterministic model comparison runs are stable
- at least one control-memory suite exists
- at least one context-ladder suite exists
- the project has a clean shortlist of models worth the full comparison run
