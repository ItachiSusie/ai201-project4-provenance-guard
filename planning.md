# Provenance Guard — Planning Document

A backend system that classifies whether submitted text is **human-written or AI-generated**,
scores its confidence honestly, shows readers a plain-language transparency label, and lets
creators appeal a classification they believe is wrong.

> **Design principle:** A false positive (flagging a real human's work as AI) is the worst
> outcome on a creative platform. Every decision below is biased toward *protecting humans*
> and *being honest about uncertainty* rather than forcing confident verdicts.

---

## Architecture

### Architecture Narrative

A single piece of text travels through the system like this:

1. A creator sends `POST /submit` with their `text` and a `creator_id`.
2. The **rate limiter** (Flask-Limiter) checks how many requests this caller has made. If they
   are over the limit, the request is rejected with `429` and goes no further.
3. The text is handed to **two independent detectors** — **Signal 1 (Groq LLM)**, a semantic
   judge, and **Signal 2 (Stylometry)**, a statistical judge. Each returns an `ai_likelihood`
   in 0–1. If Groq fails after retries, the system degrades to stylometry-only (see §6).
4. The **scorer** combines the two into one `p_ai` (weighted average) — the single source of
   truth for the verdict — and derives a user-facing `confidence` (certainty of the label shown).
5. The **label mapper** turns `p_ai` into one of three transparency labels using fixed thresholds.
6. A unique `content_id` is generated and the full record (text, scores, label, status) is saved
   to an in-memory **store**, and an entry is appended to the **audit log** (memory + `audit_log.json`).
7. The response (`content_id`, attribution, confidence, label, reason) is returned to the user.

If the creator disagrees, they send `POST /appeal` with the `content_id` and reasoning. The
system looks up that record, flips its `status` to `under_review`, writes an appeal entry to the
audit log, and returns a confirmation. No automated re-classification — a human reviews later.

> **Two-sentence summary of the flows:** The submission flow turns *raw text* into a single
> probability `p_ai` and then into exactly *one label*, so the system can never contradict
> itself. The appeal flow never re-runs detection — it only updates the stored *status* and
> records the creator's reasoning so a human reviewer can act on it later.

### Architecture Diagram

```
============================ SUBMISSION FLOW ============================

  Creator
    |  raw text + creator_id  (JSON)
    v
+--------------------+
|  POST /submit      |
+--------------------+
    |  request
    v
+--------------------+   over limit
|  Rate Limiter      |-------------------> 429 "rate limit exceeded"
|  (Flask-Limiter)   |
+--------------------+
    |  raw text  (within limit)
    |
    +-------------------------------+
    |                               |
    v                               v
+--------------------+      +----------------------+
| Signal 1: Groq LLM |      | Signal 2: Stylometry |
| (semantic judge)   |      | (structural judge)   |
+--------------------+      +----------------------+
    |  ai_likelihood (0-1)        |  ai_likelihood (0-1)
    |  + rationale                |
    +-------------+---------------+
                  v
        +--------------------------+
        |  Scorer                  |
        |  p_ai = 0.7*llm          |
        |       + 0.3*stylo        |
        |  confidence =            |
        |    max(p_ai, 1 - p_ai)   |
        +--------------------------+
                  |  p_ai (0-1)
                  v
        +--------------------------+
        |  Label Mapper            |   <-- SINGLE source of truth
        |  p_ai >=0.75 -> Likely AI|
        |  p_ai >=0.40 -> Uncertain|
        |  p_ai < 0.40 -> Likely hu|
        +--------------------------+
                  |  attribution + label text + reason
                  v
        +--------------------------+        +------------------------+
        |  Store (in-memory dict)  |------->|  Audit Log             |
        |  key = content_id        |  write |  (memory + audit_log   |
        |  {text, scores, label,   |  entry |   .json): time, scores,|
        |   status: "classified"}  |        |   p_ai, confidence,    |
        +--------------------------+        |   label, status, ...   |
                  |                         +------------------------+
                  |  content_id, attribution, confidence, label, reason
                  v
              Response (JSON)  --->  Creator
                                    (UI shows label + reason only)


============================== APPEAL FLOW ==============================

  Creator
    |  content_id + creator_reasoning  (JSON)
    v
+--------------------+
|  POST /appeal      |
+--------------------+
    |  content_id
    v
+--------------------------+   not found
|  Store lookup            |-------------------> 404 "content_id not found"
+--------------------------+
    |  found record
    v
+--------------------------+
|  status = "under_review" |
+--------------------------+
    |  appeal_reasoning + status
    v
+------------------------+
|  Audit Log             |
|  appends appeal entry  |
+------------------------+
    |  confirmation
    v
  Response (JSON)  --->  Creator
  {message, content_id, status: "under_review"}


============================ INSPECTION ENDPOINT ========================

  GET /log  --->  Audit Log  --->  { "entries": [ ...most recent first... ] }
```

---

## 1. Detection Signals

The pipeline uses **two genuinely independent signals** — one semantic, one structural — so the
combination is more informative than either alone. Each signal outputs an `ai_likelihood` in 0–1.

### Signal 1 — Groq LLM (semantic coherence)
- **What it measures:** Whether the text *reads* as human or AI when judged holistically for
  meaning, tone, and stylistic coherence.
- **Output shape:** the model (`llama-3.3-70b-versatile`, `temperature=0` for reproducible
  scores) is prompted to return structured JSON `{"ai_likelihood": <0-1>, "rationale": "<one
  sentence>"}`. We parse `ai_likelihood` (0 = clearly human, 1 = clearly AI) as the signal score
  and keep `rationale` for the `reason` field. The call is retried up to 3× on failure and its
  latency is recorded (`llm_latency_ms`); see §6 for the fallback.
- **Why it differs human vs. AI:** AI text is often fluent but "flat" — evenly polished, hedged,
  and lacking lived-in specificity. The LLM senses that overall texture.
- **Blind spot:** It is a black box that can be **fooled by a confident, formal human** (e.g., a
  non-native speaker or academic) and its judgment can drift between calls. It cannot *explain*
  its score in measurable terms.

### Signal 2 — Stylometric heuristics (structural uniformity)
- **What it measures & how each metric maps to an AI-likelihood (all results clamped to [0,1]):**
  - **Type-token ratio (TTR)** = unique words / total words. Human ≈ 0.6–0.8, AI ≈ 0.4–0.55.
    *Low diversity ⇒ AI.* Map: `ttr_ai = clamp((0.70 - ttr) / (0.70 - 0.45), 0, 1)`.
  - **Sentence-length variance** (std dev of words per sentence). Human writing varies a lot, AI
    is even. *Low variance ⇒ AI.* Map: `var_ai = clamp((6 - std) / (6 - 2), 0, 1)`
    (std ≥ 6 words → very human, std ≤ 2 → very AI).
  - **Average word length** (chars/word) — replaces punctuation density, whose direction is
    ambiguous (both AI and humans use lots of punctuation). Longer average words ⇒ more
    formal/AI-leaning. Map: `len_ai = clamp((avg_len - 4.0) / (5.5 - 4.0), 0, 1)`.
- **Output shape:** `stylo_ai_likelihood = mean(ttr_ai, var_ai, len_ai)`, a float in 0–1
  (0 = human-like variability, 1 = AI-like uniformity).
- **Short-input guard:** if the text has **fewer than 25 words**, stylometry is statistically
  unreliable — we use the LLM signal only and bias the final verdict toward **Uncertain**.
- **Why it differs human vs. AI:** AI writing tends to be **uniform** — similar sentence lengths,
  moderate vocabulary. Human writing is **messier and more variable**.
- **Blind spot:** It is **blind to meaning**. A polished, formal *human* essay can look uniform
  and score AI-ish (false-positive risk); a *casual AI* prompt can mimic messiness and score low.
  Short texts give unstable statistics (hence the guard above).

> **Note on constants:** the normalization ranges above are reasoned defaults; they may be tuned
> during M4 calibration, but any change is recorded here so the spec stays the source of truth.

### Combining into one score
```
p_ai       = 0.7 * llm_ai_likelihood + 0.3 * stylo_ai_likelihood   # single source of truth (P[AI])
confidence = max(p_ai, 1 - p_ai)                                   # certainty of the label we show
```
- **Why weighted, not equal:** The LLM is the stronger semantic judge (weight 0.7); stylometry is
  a transparent sanity-check (weight 0.3). When the two **disagree**, `p_ai` naturally lands in the
  middle "Uncertain" band — disagreement becomes honest doubt, not a coin flip.
- **Why `confidence = max(p_ai, 1 - p_ai)`:** see §2 — confidence always means "how sure we are of
  the verdict we displayed," never the raw AI-likelihood.

---

## 2. Uncertainty Representation

- **What `confidence` means (decided first, in words):** `confidence` is *how sure the system is of
  the verdict it just showed you* — **not** the AI-likelihood. It is always the probability of the
  announced class: `confidence = max(p_ai, 1 - p_ai)`. So "Likely human, confidence 0.90" means
  *90% sure it's human*; "Likely AI, confidence 0.88" means *88% sure it's AI*. This matches
  everyday intuition and never shows a confusing "16% confident" next to a human verdict.
- **`p_ai` (internal):** the combined probability the text is AI (0 = certainly human, 1 =
  certainly AI, 0.5 = cannot tell). `p_ai` decides the verdict; `confidence` is derived from it.
- **Verdict thresholds (on `p_ai`):**

  | `p_ai` range   | Attribution    | Displayed `confidence`            | Meaning |
  |----------------|----------------|-----------------------------------|---------|
  | `>= 0.75`      | `likely_ai`    | `p_ai` (≥ 0.75)                   | High-confidence AI |
  | `0.40 – 0.749` | `uncertain`    | `max(p_ai, 1 - p_ai)` (0.50–0.75) | Could not confidently decide |
  | `< 0.40`       | `likely_human` | `1 - p_ai` (> 0.60)               | High-confidence human |

- **Why the AI threshold is high (0.75) and asymmetric:** A false positive (human flagged as AI)
  is the worst outcome, so we only assert "AI" when *very* sure. The wide Uncertain band
  (0.40–0.75) absorbs borderline cases instead of accusing real humans — a deliberate bias toward
  protecting creators. "Uncertain" therefore always carries a *low* confidence (≤ 0.75), honestly
  signalling doubt.

### Worked examples (confidence = certainty of the shown verdict)

| Text | `p_ai` | Verdict | Displayed `confidence` |
|------|--------|---------|------------------------|
| Clear AI essay | 0.88 | Likely AI | 0.88 |
| Casual review | 0.10 | Likely human | 0.90 |
| Maria's formal essay | 0.68 | Uncertain | 0.68 |
| Slightly human-leaning | 0.35 | Likely human | 0.65 |

### False-Positive Trace (why these thresholds matter)
Maria, a non-native English speaker, submits a formal personal essay she wrote herself.
1. `POST /submit` passes the rate limiter.
2. Groq: "polished, formal" -> `ai_likelihood = 0.70`.
3. Stylometry: uniform sentences, modest vocabulary -> `ai_likelihood = 0.62`.
4. `p_ai = 0.7*0.70 + 0.3*0.62 = 0.676` -> **Uncertain** (not the >=0.75 AI band);
   `confidence = max(0.676, 0.324) = 0.68`.
5. Label shown: the *Uncertain* variant — gentle, non-accusatory.
6. Maria appeals -> status `under_review`, appeal logged.

Because `p_ai` *reflects* the doubt and the label *communicates* it softly, a borderline human is
never confidently accused — and always has an appeal path.

---

## 3. Transparency Label Design

The label is what a **non-technical reader** sees. It states the verdict in plain language, makes
the confidence meaningful, and (for AI/uncertain) flags that the result is automated and
appealable. The submission endpoint returns whichever variant matches the `p_ai` band.

### Variant A — High-confidence AI (`p_ai >= 0.75`)
> **🤖 Likely AI-generated.** Our automated analysis found strong signals that this text was
> produced by an AI system. This is an automated assessment and is not guaranteed to be correct.
> If you are the creator and believe this is wrong, you can appeal this result.

### Variant B — High-confidence human (`p_ai < 0.40`)
> **✍️ Likely human-written.** Our automated analysis found strong signals that a person wrote
> this text, with no meaningful indication of AI generation.

### Variant C — Uncertain (`0.40 <= p_ai < 0.75`)
> **❓ Origin uncertain.** Our system could not confidently determine whether this text was
> written by a person or generated by AI. Please treat this result as inconclusive. If you are
> the creator, you can appeal to have it reviewed.

**Design note:** Variants A and C explicitly mention the appeal path because those are the cases
that can harm a creator. Variant B does not accuse anyone, so no appeal prompt is needed (an
appeal is still technically possible via the API).

---

## 4. Appeals Workflow

- **Who can appeal:** the creator of a submission (anyone holding the `content_id` returned by
  `/submit`). No authentication in this project; in production this would be tied to the
  authenticated creator account.
- **What they provide:** `content_id` (which decision) + `creator_reasoning` (free text
  explaining why they disagree).
- **What the system does on receipt:**
  1. Look up the record by `content_id` (404 if not found).
  2. Update its `status` from `classified` -> `under_review` in the store.
  3. Append an **appeal entry** to the audit log containing the original decision (scores, label)
     **plus** the `creator_reasoning` and a fresh timestamp.
  4. Return a confirmation `{message, content_id, status: "under_review"}`.
- **No automated re-classification** — the verdict is not recomputed.
- **What a human reviewer sees in the appeal queue:** every record with `status == "under_review"`,
  showing the original `text`, both signal scores, `p_ai`, the displayed `confidence`, the assigned
  `label`, the timestamps, and the creator's `appeal_reasoning` — enough context to manually
  confirm or overturn the decision. (Reviewers can pull this via `GET /log` filtered to
  `under_review`.)

---

## 5. Anticipated Edge Cases

Specific content types this system will likely handle poorly, tied to concrete signal weaknesses:

1. **Repetitive simple-vocabulary poetry / song lyrics (false positive).** A poem built on
   repetition ("I rise, I rise, I rise") and short uniform lines produces a **low type-token
   ratio** and **low sentence-length variance** — exactly the stylometric fingerprint of AI
   uniformity. Signal 2 pushes `p_ai` up and the human poet risks being flagged. The high 0.75
   threshold mitigates but does not eliminate this.

2. **Formal non-native or academic human writing (false positive).** A grammatically careful,
   evenly structured essay from a non-native speaker reads "polished and flat" to the LLM and
   "uniform" to stylometry, so *both* signals lean AI even though a human wrote it. This is the
   Maria scenario — the reason the Uncertain band is intentionally wide.

3. **Very short inputs (unstable, low information).** A haiku, a tweet, or a one-line caption
   gives stylometry too few sentences/words for stable statistics, and the LLM little to judge.
   The `<25`-word guard (§1) forces these toward **Uncertain** instead of a confident verdict.

4. **Lightly human-edited AI text (genuinely ambiguous).** Text generated by AI then lightly
   reworded by a human is a true gray area — by design it should land mid-range (Uncertain)
   rather than be forced into a confident bucket.

---

## 6. Reliability, Fallback & Logging

- **LLM retries:** the Groq call is a single request/response (not an agent loop). On failure
  (timeout, 5xx, rate limit) it is retried up to **3×** with short backoff.
- **Fallback (graceful degradation):** if Groq still fails, run **stylometry-only**, force the
  verdict to **`uncertain`**, and tag the audit entry `"degraded": true`. The system stays up and
  the reduced-quality decision is honestly marked. (Alternative stricter mode: return `503`.)
- **Determinism:** `temperature=0` so the same text yields the same score — required for the two
  reproducible example scores in the README. It does *not* bias the judge toward "human"; for a
  scoring task it only removes run-to-run jitter without changing accuracy.
- **Latency:** each submission records `llm_latency_ms` for monitoring.
- **Audit persistence:** every entry is appended to **`audit_log.json`** (structured JSON, not
  `print`) as well as kept in memory, so entries survive restarts and are easy to surface via
  `GET /log` and paste into the README.
- **The `reason` field:** combines Signal 1's natural-language `rationale` (from Groq) with a
  templated Signal 2 summary, e.g. *"LLM: even, formal phrasing with little personal specificity.
  Stylometry: low sentence variance + low vocabulary diversity (uniform, AI-like)."* The canonical,
  reproducible record is the structured score set; `reason` is the human-readable explanation kept
  for the appeal reviewer and end user (and for explainability/audit compliance).

---

## 7. Rate Limiting

- **Chosen limits:** `10 per minute; 100 per day`, keyed per client IP (Flask-Limiter,
  `memory://` storage).
- **Reasoning:** a real creator submits their own work a handful of times per hour at most, so
  `10/minute` never blocks legitimate use, while a script trying to flood the classifier hits the
  wall almost immediately. `100/day` caps sustained abuse from a single source without throttling a
  prolific-but-honest writer. Limits are intentionally generous for humans, tight for bots —
  consistent with the "protect creators" bias.
- **Behavior on exceed:** respond `429` with `{"error": "Rate limit exceeded. Try again later."}`.

---

## API Surface (the contract)

### `POST /submit`
```jsonc
// INPUT
{ "text": "<the content>", "creator_id": "<who submitted>" }
// OUTPUT 200  (confidence = certainty of the announced verdict, not raw AI-likelihood)
{ "content_id": "<uuid>", "attribution": "likely_ai | uncertain | likely_human",
  "confidence": 0.90, "label": "<full transparency label text>",
  "reason": "<short explanation>", "timestamp": "<ISO-8601>" }
// OUTPUT 429
{ "error": "Rate limit exceeded. Try again later." }
```

### `POST /appeal`
```jsonc
// INPUT
{ "content_id": "<uuid>", "creator_reasoning": "<why they disagree>" }
// OUTPUT 200
{ "message": "Appeal received. Status updated to under review.",
  "content_id": "<uuid>", "status": "under_review" }
// OUTPUT 404
{ "error": "content_id not found" }
```

### `GET /log`
```jsonc
// OUTPUT 200
{ "entries": [ { /* full audit entry, most recent first */ } ] }
```

### Audit entry shape
```jsonc
{
  "content_id": "<uuid>", "creator_id": "test-user-1", "timestamp": "<ISO-8601>",
  "text": "<full submitted text, not truncated>",
  "llm_ai_likelihood": 0.70, "stylo_ai_likelihood": 0.62,
  "p_ai": 0.676, "confidence": 0.68, "attribution": "uncertain",
  "label": "<label text>",
  "reason": "<LLM rationale + templated stylometry summary, truncated to ~200 words>",
  "llm_latency_ms": 412, "degraded": false,
  "status": "classified | under_review", "appeal_reasoning": null
}
```

---

## AI Tool Plan

How each implementation milestone will use this spec to prompt an AI tool, and how output is
verified before it is trusted.

### M3 — Submission endpoint + first signal (Groq)
- **Spec sections provided:** §1 Detection Signals (Signal 1) + the Architecture Diagram +
  the API Surface (`/submit`, `/log`).
- **Ask the AI to generate:** (1) a Flask app skeleton with the `POST /submit` route stub and a
  `GET /log` endpoint, and (2) the `groq_signal(text) -> {ai_likelihood, rationale}` function
  (`temperature=0`, retry ×3), plus a JSON audit-log helper that also appends to `audit_log.json`.
- **How I verify:** call `groq_signal()` directly on 3–4 sample texts and inspect the raw
  `ai_likelihood` + `rationale` *before* wiring it into the route; confirm it returns a 0–1 value
  (not a label) and the route matches the API contract; run the curl test and check `content_id`
  plus a structured entry in `audit_log.json` appear.

### M4 — Second signal + confidence scoring
- **Spec sections provided:** §1 Detection Signals (Signal 2 formulas) + §2 Uncertainty
  Representation (thresholds + weights) + the Architecture Diagram.
- **Ask the AI to generate:** (1) the `stylo_signal(text) -> float` function using the **exact
  normalization formulas in §1** (TTR, sentence-length variance, average word length; clamped) with
  the `<25`-word guard, and (2) `combine(llm, stylo) -> p_ai`, `confidence = max(p_ai, 1 - p_ai)`,
  and `classify(p_ai) -> attribution`.
- **What I check:** that the generated thresholds **exactly match** §2 (0.75 / 0.40) and the
  normalization constants match §1 — AI tools often invent plausible-but-wrong cutoffs; test the 4
  calibration inputs (clear AI, clear human, two borderline) and confirm `p_ai` varies meaningfully
  and lands in the intended bands, and that `confidence` reads as certainty of the shown verdict.
  If a result is off, print `llm_ai_likelihood` and `stylo_ai_likelihood` separately to find the
  misbehaving signal.

### M5 — Production layer (label + appeals + rate limit + full log)
- **Spec sections provided:** §3 Transparency Label Design (all three variants) + §4 Appeals
  Workflow + §6 Reliability + §7 Rate Limiting + the Architecture Diagram + the API Surface
  (`/appeal`).
- **Ask the AI to generate:** (1) a `make_label(p_ai) -> (attribution, label_text)` function
  mapping each band to the **exact** variant text in §3, (2) the `POST /appeal` endpoint that looks
  up `content_id`, sets `status = under_review`, and logs the appeal, and (3) Flask-Limiter wired
  with `10/minute; 100/day` (§7) plus the retry/fallback wrapper (§6).
- **How I verify:** ask the tool to print all three variants and diff them against §3 verbatim;
  submit inputs that reach each band to confirm all three labels are reachable; run the appeal
  curl with a real `content_id` and confirm `GET /log` shows `status: under_review` with
  `appeal_reasoning` populated; send 12 rapid requests and confirm the 11th–12th return `429`.

---

## Stretch Features
*(Update this section before starting any stretch feature.)*

- [ ] Ensemble detection (3+ signals with documented weighting)
- [ ] Provenance certificate ("verified human" credential)
- [ ] Analytics dashboard (detection patterns, appeal rates)
- [ ] Multi-modal support (second content type)

---

## AI Usage Log
*(Updated throughout the project.)*

| Step | Tool used | What I asked it to do | What I changed |
|---|---|---|---|
| | | | |
