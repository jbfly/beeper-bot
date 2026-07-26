# Control-chat memory and eval plan

## 1. Goal

Extend the bot from archive QA into a control-chat assistant with bounded conversational memory, explicit user-approved facts, and stable regression tests for both.

Current deterministic baseline on the live local Gemma stack:

- `starter`: `8/8` scored passed, plus `2` diagnostic/info cases
- `core`: `20/20` scored passed
- `slice`: `14/14` scored passed
- `control-memory`: `3/3` scored passed
- `context-ladder`: `9/9` scored passed, with `3` stress cases still metrics-only

This matters because the earlier harness was no longer measuring the current architecture. The present baseline now reflects bounded chat-window reasoning, explicit source attribution, and first-pass control-memory behavior.

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
   - implemented 2026-06-13: `maybe_refresh_control_summary` folds turns
     older than the 8-turn verbatim window into a ≤120-word LLM-maintained
     summary (refresh every 6 accumulated old turns, cursor in
     `runtime_state`), run by the bridge after replied exchanges

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

### 4.3.1 Determinism caveat

Temperature 0 does not make runs fully reproducible on the local stack:
`llama-server` KV-cache reuse (`CACHE_REUSE=256`) changes logits slightly
depending on what was cached by earlier cases, so borderline cases can flip
between runs (observed: ±1 case on `slice`). Treat single-case deltas as
noise; for high-stakes comparisons either run suites twice or serve evals
with cache reuse disabled.

### 4.4 Answer-path validity

The harness serves two different purposes, and a case must declare which one it is:

- product-routing cases: verify deterministic application behavior such as
  structured memory writes, confirmation handling, and command parsing.
  Deterministic code paths are acceptable and often correct here.
- model-scored cases: verify model behavior. These must reach the LLM.
  A case that is answered by a deterministic shortcut scores zero signal
  for model comparison, no matter what the answer text says.

Rules:
- the harness records the answer path per case: `direct` (no LLM call) or
  `model` (planner and/or answer call made), using the existing trace events
- model-scored suites fail a case that resolved via the `direct` path,
  even if the answer text matches
- model shootout summaries count only model-path cases
- no question-literal string matching in `src/`: if an eval question's exact
  phrasing appears in application code, the case is overfit and must be
  rewritten or the code generalized
- each model-scored behavior needs held-out paraphrase variants so a regex
  shim cannot quietly satisfy the suite
- source-class expectations must be verifiable: archive use shows up as a
  valid citation, control-turn and memory use as content overlap with the
  fixture. "summary" was dropped as an expected source (2026-06-13) — a
  concise correct answer needn't echo the rolling summary's wording, so
  token-overlap inference punished exactly the right behavior
- ladder rungs must keep the intent resolvable at every rung: pressure
  comes from clutter, never from introducing a competing referent. Genuine
  ambiguity is its own behavior (surface candidates or ask) and gets its
  own case (`pronoun_ambiguous_lists_candidates`), not a ladder rung

Status note: the first-pass control-memory and ladder results predated these
rules and were scored against deterministic shortcut paths. After the
de-shim (2026-06-12) the honest 26B baseline was starter 6/8, core 18/20,
slice 14/14, control-memory 8/8, context-ladder 4/9.

Update (2026-06-12, later): after switching the active model to Gemma 4
12B Q6_K and landing the porter-stemming FTS migration (schema v5), the
baseline is clean on all low-pressure suites — starter 8/8, core 20/20,
slice 14/14, control-routing 3/3, control-memory 8/8, catchup 5/5 —
verified identical across two consecutive runs. The borderline archive
failures (`anna_owed_john` class) were FTS morphology misses, now fixed.
Context-ladder stands at 3/9 with families failing at the medium rung;
that is the open frontier this harness exists to measure. One guard was
added along the way: the single-sender hard restriction only applies to
senders that actually exist in the archive, because the planner can invent
descriptive senders like "the Sample Bay host".

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
- Alex is my sister
- Morgan is my brother in law
- Jordan is my partner
- Sunny is an alias for Sam Rivera

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

This suite exists to answer a different question from `starter` and `slice`.
Those suites measure low-pressure answer quality. The ladder measures quality under prompt pressure.
Use it to compare a stronger larger model against smaller models that may leave more room for working context.

Run the same intent over several context conditions:
- recent turns only
- recent turns plus rolling summary
- longer thread with summary refresh
- stress case near the configured budget

Keep the user intent fixed across rungs. Grow only the prompt burden.
The burden may come from:
- more control turns
- larger rolling summaries
- larger structured memory state
- larger retrieved evidence packets
- distractor turns and topic detours

Record where failures begin.
The point is not perfect recall of arbitrary long threads. The point is stable bounded behavior.

### 5.5 Retrieval isolation

For mixed memory questions, verify source use.

Examples:
- "Who is Sunny again, and what address did they send?"
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
- source classes used per case where the harness can infer them
- first failed context rung per ladder family
- whether the run hit truncation, clipping, or prompt-budget fallback

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
- pending confirmations expire: a confirmation only applies to the
  immediately preceding proposal; any other intervening message cancels or
  re-prompts, and stale pending rows are never applied by a later bare "yes"
- confirmation and rejection detection must tolerate punctuation and casing,
  not exact-string match a fixed phrase list
- a proposed write travels as structured data on the ask response
  (kind, subject, object, source text), not as a magic substring in the
  reply text that the bridge re-parses

### 8.4 One canonical identity store

People, aliases, and identity facts live in the people graph only.
The `facts` table stores non-identity facts (relationships to the user,
preferences, durable notes) and may reference a person id.
Do not write the same alias to two stores; they will diverge.

## 9. Model selection policy for this phase

Use a small prescreen before the full shootout.
A candidate model should survive all of these before it enters the main comparison set:
- loads cleanly at the target context size
- full or acceptable GPU offload on the 16 GB card
- passes a smoke question
- produces usable scores on `starter`
- shows enough headroom or quality to justify deeper testing

Do not treat `starter` alone as the full selection test.
The real comparison has two axes:
- low-pressure quality: `starter`, `core`, `slice`
- context-pressure behavior: `context-ladder`

A smaller model may remain in the field even if it loses `starter`, but only if:
- it holds 32k cleanly
- it leaves materially more headroom
- and its failure point on the ladder is later or gentler than the larger baseline

The main comparison set should then contain:
- the current Gemma baseline
- one or two headroom-friendly models
- one stronger stretch model only if runtime remains practical

Current reading after the first prescreen pass:
- Gemma 4 26B q4 remains the quality baseline
- Qwen3 14B Q6_K is the main smaller-model challenger worth ladder testing
- Phi-4 is less attractive because the tested runtime clipped it to 16k
- Mistral Nemo 12B and Qwen3 14B Q5_K_M do not look strong enough for deeper comparison unless used as headroom references only

## 10. Immediate next tasks

Done so far: deterministic eval mode, extended eval schema, first
control-memory and context-ladder suites, first prescreen pass, and a
first-pass memory substrate (control turns, facts, pending updates).

Revised order:

1. de-shim the control-memory path
   - delete question-literal rewrites in `_rewrite_followup_question`
   - route follow-up resolution through the planner using `control_context`
     (the plumbing already exists)
   - keep `_direct_memory_answer` and `_direct_memory_write_answer` only as
     explicit product-routing behavior, never as a way to pass model suites
2. implement answer-path recording per section 4.4 and make model-scored
   suites fail `direct`-path resolutions
3. split suites: a `control-routing` suite for deterministic product
   behavior, and model-scored `control-memory` cases with held-out
   paraphrase variants (target 10 to 15 cases before trusting any score)
4. harden the confirmation flow per section 8.3 (structured proposed
   actions, expiry, punctuation-tolerant confirmation)
5. collapse identity facts into the people graph per section 8.4
6. re-baseline all suites on the current Gemma stack with the honest paths
7. refresh the candidate shortlist against what is currently available
   before committing prescreen time, then run the prescreen and shootout

## 11. Candidate model directions

Prioritize models that either:
- may beat Gemma on archive and slice tasks, or
- come close while leaving more headroom for control-chat memory

Current shortlist for the first prescreen pass:
- Phi-4 Q5_K_M
- Mistral Nemo 12B Instruct Q5_K_M
- Qwen3 14B Q5_K_M
- Qwen3 14B Q6_K
- optional headroom baseline: Gemma-3n E4B Q8_0
- optional wildcard later: GLM-4.5 Air, only if the runtime path looks clean

Reason:
- all primary candidates fit the 16 GB GPU more comfortably than the current Gemma 26B setup
- all primary candidates leave room for larger control-chat working sets
- none of the primary candidates are obvious barely-fits experiments

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
- the context-ladder suite has short, medium, long, and stress rungs with fixed-intent comparisons
- the project has a clean shortlist of models worth the full comparison run

## 13. Multi-model runtime note

A mixed-model design is feasible, but it should not be the first memory implementation.
Use it only if one model is clearly better at low-pressure answer quality and another is clearly better under context pressure.

The practical path on this machine is sequential loading, not two resident large models.
The current 16 GB card cannot keep two 26B-class servers alive at once, and even a split planner plus answer setup with the same 26B model was already shown to be impractical.

A later mixed-model policy could look like this:
- use a smaller 32k model for long control-chat continuity, summary refresh, and memory-update routing
- escalate to the stronger larger model for hard archive QA or high-value slice reasoning
- keep the control chat informed when a slow escalation is happening

The costs are real:
- model swap latency
- prompt-state handoff complexity
- more caching and orchestration logic
- more evaluation modes
- more places for source confusion or regressions

So the near-term order stays:
- first build one clean memory substrate
- first prove the context-ladder harness
- then consider a two-tier policy if the ladder shows a real tradeoff
