# ai201-project4-provenance-guard

Provenance Guard is a Flask backend for classifying submitted text as likely AI-generated, likely human-written, or uncertain. It combines two detection signals, turns them into a calibrated confidence score, returns a plain-language transparency label, supports creator appeals, rate-limits abusive traffic, and writes a structured audit log.

The main design principle is creator protection: a false positive that labels a real person's work as AI-generated is the most harmful outcome, so the system uses a high AI threshold and a wide uncertain band instead of forcing a binary verdict.

## Architecture overview

A submission moves through the system in this order:

1. A creator sends `POST /submit` with `text` and `creator_id`.
2. Flask-Limiter checks the client IP. Requests over the limit return `429` before detection runs.
3. The text goes to two independent detectors:
   - Signal 1: Groq LLM semantic judgment.
   - Signal 2: stylometric structural heuristics.
4. The scorer combines both signal scores into one internal `p_ai`.
5. The label mapper converts `p_ai` into one of three transparency labels.
6. The API returns `content_id`, `attribution`, `confidence`, `label`, `reason`, and `timestamp`.
7. The full decision is written to `data/audit_log.json`.

If the creator appeals, `POST /appeal` looks up the `content_id`, updates the decision status to `under_review`, records the creator's reasoning, and appends an appeal event to the audit log. It does not re-run detection; a human reviewer would use the stored scores and appeal text.

## Detection signals

### Signal 1: Groq LLM semantic judgment

`groq_signal(text)` asks `llama-3.3-70b-versatile` to return structured JSON with `ai_likelihood` from 0 to 1 and a one-sentence rationale. This signal measures the whole-text reading: tone, coherence, generic phrasing, hedging, and whether the writing feels polished but flat.

I chose this signal because AI-generated writing often has semantic and tonal patterns that are hard to capture with simple counts. What it misses: it is a black-box judgment, can drift with model behavior, and can mistake formal human writing for AI-like prose.

### Signal 2: stylometric heuristics

`stylo_signal(text)` computes transparent metrics:

| Metric | What it measures | AI-like direction |
|---|---|---|
| Type-token ratio | Vocabulary diversity | Lower diversity can indicate AI-like repetition |
| Sentence-length standard deviation | Variation in sentence lengths | Lower variation can indicate uniform generated prose |
| Average word length | Formality / lexical complexity | Longer average words can lean formal or AI-like |

I chose stylometry because it is independent from the LLM: it does not understand meaning, but it gives measurable structural evidence. What it misses: it can be fooled by polished human essays, repetitive poetry, or casual AI text that intentionally imitates messy human style. For texts under 25 words, stylometry is marked unreliable because there are too few words and sentences for stable statistics.

For a real deployment, I would calibrate these constants on a labeled validation set, add language- and genre-specific baselines, monitor appeal outcomes, store records in a database, and add authentication around creator identity.

## Confidence scoring

Both signals output an AI-likelihood score in `[0, 1]`. The combined score is:

```text
p_ai = 0.7 * llm_ai_likelihood + 0.3 * stylo_ai_likelihood
confidence = max(p_ai, 1 - p_ai)
```

The LLM gets more weight because it is the stronger semantic judge. Stylometry gets a smaller weight because it is transparent and useful as a sanity check, but easier to fool. The final `confidence` is not raw AI-likelihood; it is how certain the system is about the label it displays.

| `p_ai` range | Attribution | Meaning |
|---|---|---|
| `>= 0.75` | `likely_ai` | Strong enough evidence to label AI-generated |
| `0.40 <= p_ai < 0.75` | `uncertain` | Ambiguous or disagreeing signals |
| `< 0.40` | `likely_human` | Strong enough evidence to label human-written |

The AI threshold is intentionally high because false accusations are worse than uncertainty. The uncertain band is wide so disagreement between the LLM and stylometry becomes visible instead of being hidden behind an overconfident label.

### Example scores

These examples come from local Milestone 4/5 validation and show the score is not constant.

| Case | LLM score | Stylometry score | `p_ai` | Displayed confidence | Attribution |
|---|---:|---:|---:|---:|---|
| Casual ramen review: "ok so i finally tried that new ramen place..." | 0.12 | 0.061 | 0.102 | 0.898 | `likely_human` |
| Lightly edited remote-work paragraph: "I've been thinking a lot about remote work lately..." | 0.55 | 0.348 | 0.489 | 0.511 | `uncertain` |
| Polished AI-like paragraph about AI ethics | 0.92 | 0.38 | 0.758 | 0.758 | `likely_ai` |

The high-confidence human example has confidence `0.898`; the lower-confidence ambiguous example has confidence `0.511`, showing meaningful variation.

## Transparency label variants

The submission endpoint returns exactly one of these label texts based on the `p_ai` band:

| Variant | Exact displayed text |
|---|---|
| High-confidence AI | "**🤖 Likely AI-generated.** Our automated analysis found strong signals that this text was produced by an AI system. This is an automated assessment and is not guaranteed to be correct. If you are the creator and believe this is wrong, you can appeal this result." |
| High-confidence human | "**✍️ Likely human-written.** Our automated analysis found strong signals that a person wrote this text, with no meaningful indication of AI generation." |
| Uncertain | "**❓ Origin uncertain.** Our system could not confidently determine whether this text was written by a person or generated by AI. Please treat this result as inconclusive. If you are the creator, you can appeal to have it reviewed." |

## Run locally

```powershell
Set-Location C:\CodePath\ai201-project4-provenance-guard
.\.venv\Scripts\python.exe app.py
```

Set `GROQ_API_KEY` before running if you want the live LLM signal. If the LLM signal is unavailable, the app degrades gracefully, records `"degraded": true`, and keeps the verdict in the uncertain band.

## API usage

### Submit text

```powershell
$body = @{
  text = "Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that stakeholders must collaborate to ensure responsible deployment."
  creator_id = "test-user-1"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri http://localhost:5000/submit `
  -Method Post `
  -ContentType "application/json" `
  -Body $body | ConvertTo-Json -Depth 10
```

### Appeal a decision

```powershell
$appeal = @{
  content_id = "PASTE-CONTENT-ID-HERE"
  creator_reasoning = "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical."
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri http://localhost:5000/appeal `
  -Method Post `
  -ContentType "application/json" `
  -Body $appeal | ConvertTo-Json -Depth 10
```

Successful appeal response:

```json
{
  "content_id": "45b430d3-e0c1-4328-afcc-fa14cdb7d24f",
  "message": "Appeal received. Status updated to under review.",
  "status": "under_review"
}
```

Unknown IDs return:

```json
{ "error": "content_id not found" }
```

### View the audit log

```powershell
Invoke-RestMethod http://localhost:5000/log | ConvertTo-Json -Depth 10
```

## Rate limiting

`POST /submit` uses Flask-Limiter with `memory://` storage and client-IP keys:

- `10 per minute`
- `100 per day`

These limits are meant to allow realistic creator usage while blocking automated flooding. A creator submitting their own work should not need more than 10 classifications in a minute, but a script can easily exceed that. The `100/day` limit caps sustained abuse without blocking a prolific but legitimate writer.

Rate-limit exceeded response:

```json
{ "error": "Rate limit exceeded. Try again later." }
```

Validation evidence from 12 rapid submissions from the same client:

```text
200
200
200
200
200
200
200
200
200
200
429
429
```

## Audit log evidence

The canonical structured audit log is `data/audit_log.json`, not console output. Entries include timestamp, content ID, attribution, confidence, both individual signal scores, `p_ai`, status, and appeal reasoning.

Representative entries:

```json
[
  {
    "event_type": "submission",
    "timestamp": "2026-07-01T05:39:51.837Z",
    "content_id": "45b430d3-e0c1-4328-afcc-fa14cdb7d24f",
    "attribution": "likely_ai",
    "confidence": 0.758,
    "llm_ai_likelihood": 0.92,
    "stylo_ai_likelihood": 0.38,
    "p_ai": 0.758,
    "status": "under_review",
    "appeal_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical."
  },
  {
    "event_type": "submission",
    "timestamp": "2026-07-01T05:39:51.870Z",
    "content_id": "bdec5649-69af-4825-b8b9-55cd83e0594b",
    "attribution": "likely_human",
    "confidence": 0.898,
    "llm_ai_likelihood": 0.12,
    "stylo_ai_likelihood": 0.061,
    "p_ai": 0.102,
    "status": "classified",
    "appeal_reasoning": null
  },
  {
    "event_type": "appeal",
    "timestamp": "2026-07-01T05:39:51.935Z",
    "content_id": "45b430d3-e0c1-4328-afcc-fa14cdb7d24f",
    "attribution": "likely_ai",
    "confidence": 0.758,
    "llm_ai_likelihood": 0.92,
    "stylo_ai_likelihood": 0.38,
    "p_ai": 0.758,
    "status": "under_review",
    "appeal_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical."
  }
]
```

## Known limitations

- **Formal non-native or academic human writing:** Both signals can over-score this as AI-like. The LLM may see polished phrasing as generic, and stylometry may see uniform sentence structure and longer words as AI-like. The wide uncertain band is meant to reduce harm here.
- **Repetitive poetry or song lyrics:** Repetition lowers type-token ratio and repeated line structures lower sentence-length variance, which can look like AI-style uniformity even when the work is human.
- **Very short text:** A caption, haiku, or one-line post does not provide enough words for stable stylometry. The short-input guard marks stylometry unreliable and pushes toward uncertainty.
- **Lightly edited AI output:** Human edits can add casual phrasing while leaving generated structure intact, so both signals may reasonably disagree.

## Spec reflection

One way the spec helped: the architecture diagram and fixed thresholds kept the implementation consistent. `p_ai` is the single source of truth for labels, confidence, audit logging, and appeals, so the API does not contradict itself. The spec's false-positive warning also directly shaped the high `0.75` AI threshold and the wide uncertain band.

One way the implementation diverged: the plan described an in-memory store plus `audit_log.json`, but the implementation uses `data/audit_log.json` as the practical persistent store for both logging and appeal lookup/update. For this project size, a JSON file is easier to inspect and sufficient for grading evidence; in production I would replace it with a database and explicit review queue.

## AI usage

| Instance | What I directed the AI to do | What it produced | What I revised or overrode |
|---|---|---|---|
| Milestone 4: second signal and scoring | Generate a standalone stylometry signal and confidence scoring logic from the detection-signal and uncertainty sections of `planning.md`. | A plan/code structure for `stylo_signal()`, weighted scoring, confidence, and label classification. | Verified and corrected the exact constants: TTR normalization, sentence-variance normalization, average-word-length normalization, `0.7/0.3` weights, and `0.75/0.40` thresholds. |
| Milestone 5: production layer | Generate label mapping, `POST /appeal`, Flask-Limiter setup, and complete audit-log fields. | A production-layer implementation plan and code edits for appeals, rate limiting, and structured logging. | Added structured appeal entries that preserve original decision fields, updated the original submission status to `under_review`, and validated rate limiting with 12 rapid requests. |
| README evidence | Use the implemented project and audit-log samples to organize the README evidence. | A README outline covering labels, appeals, rate limits, and audit logs. | Manually aligned examples with real `data/audit_log.json` entries and added design reasoning, limitations, and spec reflection. |

## Portfolio walkthrough

A short walkthrough can follow `walkthrough_script.txt`. The recording should be about 2-3 minutes and show:

1. The README overview and architecture section.
2. The Flask app running locally.
3. A `POST /submit` request and the returned `content_id`, `attribution`, `confidence`, `label`, and `reason`.
4. `GET /log` or `data/audit_log.json` showing both signal scores, `p_ai`, confidence, and status.
5. A `POST /appeal` request using the returned `content_id`, followed by the log showing `status: under_review` and populated `appeal_reasoning`.
6. The rate-limit evidence showing the 11th and 12th rapid requests return `429`.
7. Two design decisions: the wide uncertain band protects creators, and the two-signal pipeline makes disagreement visible instead of hiding it.

Recording status: script prepared in `walkthrough_script.txt`; record the final video from that script for portfolio submission.
