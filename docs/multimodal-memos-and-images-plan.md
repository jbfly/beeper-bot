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

## 5. Smoke test results (2026-06-12)

Setup: `gemma-4-12b-it-Q6_K.gguf` (9.8 GB) plus `mmproj-F16.gguf` from
`unsloth/gemma-4-12b-it-GGUF`, downloaded to
`~/models/hf/unsloth/gemma-4-12b-it-GGUF/`, registered as
`ai-ops/llama-serve/models/gemma4_12b_q6k.env`
(alias `gemma4-google-12b-q6_k-local`), served by the existing llama.cpp
build (b9566, 2026-06-08) behind the 8090 proxy. Switch with
`ai-model gemma4_12b_q6k`; switch back with `ai-model gemma4`.

- text: instruction-following smoke passed
- image: read both lines of a rendered text image verbatim
  ("MEET AT PIER 39", "CODE: 7421")
- audio: OpenAI-compatible `input_audio` (16 kHz mono WAV) works through
  the proxy; a deliberately hard espeak-synthesized memo transcribed with
  one substitution error ("dry cleaning" for "olive oil"), the rest
  verbatim

All three modalities work through the unchanged `llama-server` API, so the
bot can reach them with the existing `OpenAiCompatLlmClient` plus an
`input_audio`/`image_url` content-part extension.

## 6. Attachment access (answered 2026-06-12)

The Beeper Desktop API fully supports attachment download:

- some attachments carry `srcURL: file:///home/.../BeeperTexts/media/...`
  — already on disk, readable directly (all sampled paths existed)
- the rest carry `mxc://` URLs with an inline `encryptedFileInfoJSON`;
  `GET /v1/assets/serve?url=<urlencoded mxc>` downloads **and decrypts**
  them (verified on a real 1.5 MB encrypted WhatsApp voice memo)
- voice memos are Ogg Opus mono 48 kHz; ffmpeg converts to the 16 kHz mono
  WAV the model wants
- a real 3m20s memo chunk transcribed cleanly through the 12B

## 7. Implementation (landed 2026-06-12)

- schema v6: `attachment_derived_text` (provenance: attachment id, model
  alias, chunk count, duration, status, error)
- `media.py`: `fetch_attachment` (file:// or assets/serve with an
  on-disk cache), chunked transcription (28 s windows, 2 s overlap, max 20
  chunks ≈ 8.7 min, truncation note beyond that), image description with
  verbatim-text quoting, derivation passes with done/failed/skipped status
- derived text is written into `messages.text` and the FTS row, so
  retrieval, slice windows, catchup digests, and evals all see transcripts
  with zero changes; sync re-applies derived text after upserts overwrite
  media rows
- CLI: `beeper-bot index-media --kind voice|image --limit N [--chat X]`
- trace events carry metadata only, never base64 payloads

## 8. Open questions

- image pass at scale: 1,291 images x ~6 s is hours of GPU time; run in
  batches (`--limit`), recent chats first, and decide whether stickers/GIFs
  stay excluded
- whether to auto-derive new voice memos in the serve loop (blocks the
  poll while transcribing) or via a timer
- eval suite for media-derived answers, following plan §4.4 answer-path
  rules
