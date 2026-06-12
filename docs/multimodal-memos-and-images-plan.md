# Voice memos and image understanding plan

## 1. Goal

Extend the archive beyond text: transcribe voice memos, describe and search
images, and answer questions from both, while keeping storage and inference
local.

Candidate runtime: Gemma 4 12B (encoderless omni), released 2026-06-03.
It is the first medium-sized Gemma that natively ingests audio, handles
images through a ~35M-parameter embedder, ships under Apache 2.0, runs on a
16 GB card, and is supported by stock llama.cpp with an omni GGUF plus
mmproj. One model would then cover planning, answering, transcription, and
image QA — no separate Whisper or vision stack.

Known tradeoff: dedicated ASR still beats it on raw word error rate
(Whisper LargeV3Turbo ~11.5% WER vs ~13% for Gemma 4 audio on
LibriSpeech-other). The bet is that unified reasoning over audio plus the
archive is worth a point of WER. If memo transcription quality disappoints,
fall back to a whisper.cpp sidecar for transcription only.

## 2. Architecture decisions

1. Transcripts and descriptions are archive rows, not on-demand inference.
   Ingest once at sync time, store text in `messages`-adjacent tables, index
   in FTS. Retrieval and evals then work unchanged on text.
2. The mmproj path is already proven in `ai-ops` (the 26B A4B loads one for
   vision). Audio requests go through the same OpenAI-compatible endpoint
   with `input_audio` content parts.
3. Voice memos longer than the model's audio window (~30 s) are chunked at
   16 kHz mono WAV with small overlaps and transcribed sequentially; chunks
   are joined with a cleanup pass.
4. Every derived text row records provenance: source attachment id, model
   alias, chunk index, and created-at. Derived text is evidence; citations
   point at the original message.

## 3. Work order

1. model selection and smoke test (current step)
   - shortlist a quant of `gemma-4-12b-it` GGUF
   - register an env file in `ai-ops/llama-serve/models/`
   - verify: text smoke, image description smoke, audio transcription smoke
     through `llama-server`
2. attachment ingest
   - extend sync to download voice-memo and image attachments for
     allowlisted chats into the state dir
   - new tables: `attachments`, `attachment_derived_text`
3. transcription and description pipeline
   - background pass over un-derived attachments
   - chunking for long audio; one description plus OCR-style text pull for
     images
4. retrieval integration
   - FTS rows for derived text, marked with a source class
     (`voice-memo`, `image`) so the answer prompt and evals can attribute
     them honestly
5. eval extension
   - small suite with known memos/images and expected transcript fragments,
     following the answer-path rules in
     `docs/control-chat-memory-and-eval-plan.md` §4.4

## 4. Quant choice for the 16 GB card

From the `unsloth/gemma-4-12b-it-GGUF` table:

- `Q6_K` (~9.8 GB): near-lossless, leaves ~5 GB for KV cache, mmproj, and
  audio buffers at 32k context. Default choice.
- `UD-Q5_K_XL` (~8.6 GB): the headroom option if long-context control-chat
  work needs more KV room; quality still strong.
- `Q4` class (~6.4-7.4 GB): only if we want 64k+ context experiments.

Start with `Q6_K`. The 12B also slots directly into the model-shootout
matrix as the headroom challenger against the 26B A4B baseline: the
context-ladder suite (honest baselines, post de-shim) is the comparison
instrument.

## 5. Open questions to settle during the smoke test

- llama-server audio request shape and per-request duration cap
- real transcription quality on actual Beeper voice memos (codec, noise)
- image token budget settings (`IMAGE_MIN_TOKENS`/`IMAGE_MAX_TOKENS`)
  appropriate for chat photos
- throughput: seconds of audio per second of wall time on the local card
- whether the Beeper Desktop local API exposes attachment download URLs for
  voice memos and images in indexed chats
