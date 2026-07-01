import json
import os
import re
import statistics
import time

from dotenv import load_dotenv
from groq import Groq


MODEL_NAME = "llama-3.3-70b-versatile"
MAX_RETRIES = 3


class SignalError(RuntimeError):
    pass


def clamp(value, min_value=0.0, max_value=1.0):
    return max(min_value, min(max_value, value))


def tokenize_words(text):
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text.lower())


def split_sentences(text):
    sentences = [sentence.strip() for sentence in re.split(r"[.!?]+", text) if sentence.strip()]
    return sentences or [text.strip()]


def sentence_word_counts(text):
    return [len(tokenize_words(sentence)) for sentence in split_sentences(text)]


def stylo_signal(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")

    words = tokenize_words(text)
    if not words:
        raise ValueError("text must contain at least one word")

    word_count = len(words)
    type_token_ratio = len(set(words)) / word_count
    counts = [count for count in sentence_word_counts(text) if count > 0]
    sentence_length_std = statistics.pstdev(counts) if len(counts) > 1 else 0.0
    avg_word_length = sum(len(word) for word in words) / word_count

    ttr_ai = clamp((0.70 - type_token_ratio) / (0.70 - 0.45))
    var_ai = clamp((6 - sentence_length_std) / (6 - 2))
    len_ai = clamp((avg_word_length - 4.0) / (5.5 - 4.0))
    ai_likelihood = (ttr_ai + var_ai + len_ai) / 3

    metrics = {
        "word_count": word_count,
        "type_token_ratio": round(type_token_ratio, 3),
        "sentence_length_std": round(sentence_length_std, 3),
        "avg_word_length": round(avg_word_length, 3),
        "ttr_ai": round(ttr_ai, 3),
        "var_ai": round(var_ai, 3),
        "len_ai": round(len_ai, 3),
    }

    return {
        "ai_likelihood": round(ai_likelihood, 3),
        "reliable": word_count >= 25,
        "metrics": metrics,
        "rationale": _stylometry_rationale(metrics),
    }


def _stylometry_rationale(metrics):
    findings = []
    if metrics["ttr_ai"] >= 0.6:
        findings.append("low vocabulary diversity")
    elif metrics["ttr_ai"] <= 0.3:
        findings.append("high vocabulary diversity")

    if metrics["var_ai"] >= 0.6:
        findings.append("even sentence lengths")
    elif metrics["var_ai"] <= 0.3:
        findings.append("varied sentence lengths")

    if metrics["len_ai"] >= 0.6:
        findings.append("longer average words")
    elif metrics["len_ai"] <= 0.3:
        findings.append("shorter average words")

    if not findings:
        findings.append("mixed structural signals")

    return "Stylometry found " + ", ".join(findings) + "."


def groq_signal(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")

    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise SignalError("GROQ_API_KEY is not set")

    client = Groq(api_key=api_key)
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are Signal 1 for Provenance Guard. Assess whether submitted "
                            "text is AI-generated or human-written. Return only JSON with "
                            'exactly these keys: "ai_likelihood" and "rationale". '
                            "ai_likelihood must be a number from 0 to 1, where 0 means clearly "
                            "human-written and 1 means clearly AI-generated. rationale must be "
                            "one concise sentence."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
            )
            content = response.choices[0].message.content
            return _parse_signal_response(content)
        except Exception as error:
            last_error = error
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)

    raise SignalError(f"Groq signal failed after {MAX_RETRIES} attempts: {last_error}") from last_error


def _parse_signal_response(content):
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise SignalError(f"Groq returned invalid JSON: {content}") from error

    if "ai_likelihood" not in parsed or "rationale" not in parsed:
        raise SignalError("Groq response must include ai_likelihood and rationale")

    try:
        ai_likelihood = float(parsed["ai_likelihood"])
    except (TypeError, ValueError) as error:
        raise SignalError("ai_likelihood must be numeric") from error

    if ai_likelihood < 0 or ai_likelihood > 1:
        raise SignalError("ai_likelihood must be between 0 and 1")

    rationale = str(parsed["rationale"]).strip()
    if not rationale:
        raise SignalError("rationale must be a non-empty string")

    return {"ai_likelihood": ai_likelihood, "rationale": rationale}
