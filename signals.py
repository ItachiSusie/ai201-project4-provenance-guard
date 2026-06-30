import json
import os
import time

from dotenv import load_dotenv
from groq import Groq


MODEL_NAME = "llama-3.3-70b-versatile"
MAX_RETRIES = 3


class SignalError(RuntimeError):
    pass


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
