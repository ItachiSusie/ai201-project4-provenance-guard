import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, request

import signals
from signals import SignalError


app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
AUDIT_LOG_PATH = DATA_DIR / "audit_log.json"
PLACEHOLDER_CONFIDENCE = 0.50
PLACEHOLDER_LABEL = "Preliminary provenance label based on Signal 1."


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def attribution_from_score(ai_likelihood):
    if ai_likelihood >= 0.75:
        return "likely_ai"
    if ai_likelihood >= 0.40:
        return "uncertain"
    return "likely_human"


def ensure_log_file():
    DATA_DIR.mkdir(exist_ok=True)
    if not AUDIT_LOG_PATH.exists():
        AUDIT_LOG_PATH.write_text("[]\n", encoding="utf-8")


def get_log(limit=None):
    ensure_log_file()
    with AUDIT_LOG_PATH.open("r", encoding="utf-8") as log_file:
        entries = json.load(log_file)

    entries = list(reversed(entries))
    if limit is not None:
        return entries[:limit]
    return entries


def append_log(entry):
    ensure_log_file()
    with AUDIT_LOG_PATH.open("r", encoding="utf-8") as log_file:
        entries = json.load(log_file)

    entries.append(entry)
    with AUDIT_LOG_PATH.open("w", encoding="utf-8") as log_file:
        json.dump(entries, log_file, indent=2)
        log_file.write("\n")


@app.post("/submit")
def submit():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON body is required"}), 400

    text = payload.get("text")
    creator_id = payload.get("creator_id")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "text is required"}), 400
    if not isinstance(creator_id, str) or not creator_id.strip():
        return jsonify({"error": "creator_id is required"}), 400

    try:
        signal_result = signals.groq_signal(text)
    except (SignalError, ValueError) as error:
        return jsonify({"error": "Signal 1 failed", "details": str(error)}), 502

    llm_score = signal_result["ai_likelihood"]
    attribution = attribution_from_score(llm_score)
    content_id = str(uuid4())
    timestamp = utc_timestamp()

    response = {
        "content_id": content_id,
        "attribution": attribution,
        "confidence": PLACEHOLDER_CONFIDENCE,
        "label": PLACEHOLDER_LABEL,
        "reason": signal_result["rationale"],
        "timestamp": timestamp,
    }

    append_log(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "timestamp": timestamp,
            "attribution": attribution,
            "confidence": PLACEHOLDER_CONFIDENCE,
            "llm_score": llm_score,
            "status": "classified",
        }
    )

    return jsonify(response)


@app.get("/log")
def log():
    return jsonify({"entries": get_log()})


if __name__ == "__main__":
    ensure_log_file()
    app.run(host="0.0.0.0", port=5000, debug=True)
