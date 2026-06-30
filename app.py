"""
app.py — Provenance Guard Flask application

Routes:
  POST /api/analyze  — run both signals, aggregate, persist, return scored response
  POST /api/appeal   — (stub placeholder, implemented in M5)
  GET  /api/log      — (stub placeholder, implemented in M5)

M4: Signal 2 (stylometrics) and full aggregation are live.
"""

import logging
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from database import init_db, insert_submission, insert_audit_event, get_audit_log
from labels import generate_label
from pipeline import run_llm_signal, run_stylo_signal, aggregate_signals

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# ---------------------------------------------------------------------------
# DB init on startup
# ---------------------------------------------------------------------------
with app.app_context():
    init_db()



# ---------------------------------------------------------------------------
# POST /api/analyze
# ---------------------------------------------------------------------------
@app.route("/api/analyze", methods=["POST"])
@limiter.limit("10 per minute")
def analyze():
    """
    Required JSON fields:
      content   (str) — text to analyze
    Optional:
      title     (str) — stored, not analyzed
      author_id (str) — stored for audit trail
    """
    data = request.get_json(silent=True)

    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "Missing required field: content"}), 400

    title     = data.get("title",     "")
    author_id = data.get("author_id", "")

    # ── Signal 1: Groq LLM ──────────────────────────────────────────────────
    signal1   = run_llm_signal(content)
    llm_score = signal1["llm_score"]

    # ── Signal 2: Stylometric heuristics (pure Python, no network) ───────────
    signal2    = run_stylo_signal(content)
    stylo_score = signal2["stylo_score"]

    # ── Aggregation: weighted combination + signal-gap check (spec §1 + §2) ──
    agg = aggregate_signals(content, llm_score, stylo_score)
    composite_score  = agg["composite_score"]
    confidence_level = agg["confidence_level"]
    classification   = agg["classification"]
    signal_gap       = agg["signal_gap"]
    stylo_reliable   = agg["stylo_reliable"]

    # ── Transparency label (verbatim strings from spec §3) ───────────────────
    transparency_label = generate_label(classification, confidence_level, stylo_reliable)

    # ── Identifiers + timestamp ──────────────────────────────────────────────
    content_id = str(uuid.uuid4())
    now_iso    = datetime.now(timezone.utc).isoformat()
    word_count = len(content.split())

    # ── Persist to submissions table ─────────────────────────────────────────
    insert_submission({
        "content_id":         content_id,
        "author_id":          author_id,
        "title":              title,
        "content_snippet":    content[:500],
        "classification":     classification,
        "confidence_score":   composite_score,
        "confidence_level":   confidence_level,
        "llm_score":          llm_score,
        "stylo_score":        stylo_score,
        "signal_gap":         signal_gap,
        "stylo_reliable":     1 if stylo_reliable else 0,
        "transparency_label": transparency_label,
        "status":             "active",
        "created_at":         now_iso,
    })

    # ── Persist to audit_log ─────────────────────────────────────────────────
    insert_audit_event({
        "content_id":              content_id,
        "event_type":              "analysis",
        "appeal_id":               None,
        "reason":                  None,
        "previous_classification": None,
        "previous_confidence":     None,
        "status":                  "active",
        "timestamp":               now_iso,
        # Signal scores — individual and combined
        "llm_score":               llm_score,
        "stylo_score":             stylo_score,
        "signal_gap":              signal_gap,
        "composite_score":         composite_score,
        "confidence_level":        confidence_level,
        "classification":          classification,
    })

    logger.info(
        "Analyzed content_id=%s  llm=%.4f  stylo=%.4f  composite=%.4f  "
        "gap=%s  classification=%s  confidence=%s",
        content_id, llm_score, stylo_score, composite_score,
        f"{signal_gap:.4f}" if signal_gap is not None else "N/A",
        classification, confidence_level,
    )

    # ── API response (spec §6) ───────────────────────────────────────────────
    return jsonify({
        "content_id":         content_id,
        "classification":     classification,
        "confidence_score":   composite_score,
        "confidence_level":   confidence_level,
        "transparency_label": transparency_label,
        "signals": {
            "llm_score":         llm_score,
            "llm_reasoning":     signal1["reasoning"],
            "llm_fallback":      signal1["fallback"],
            "stylo_score":       stylo_score,
            "stylo_slv":         signal2["slv_score"],
            "stylo_ttr":         signal2["ttr_score"],
            "signal_gap":        signal_gap,
            "text_length_words": word_count,
            "stylo_reliable":    stylo_reliable,
        },
        "status":    "active",
        "timestamp": now_iso,
    }), 200


# ---------------------------------------------------------------------------
# POST /api/appeal  — placeholder
# ---------------------------------------------------------------------------
@app.route("/api/appeal", methods=["POST"])
@limiter.limit("5 per minute")
def appeal():
    """Placeholder — implemented in M5."""
    return jsonify({"error": "Not implemented yet"}), 501


# ---------------------------------------------------------------------------
# GET /api/log  (also aliased as GET /log for checkpoint curl convenience)
# ---------------------------------------------------------------------------
@app.route("/api/log", methods=["GET"])
@app.route("/log", methods=["GET"])
@limiter.limit("30 per minute")
def log():
    """
    Return audit log entries as JSON, joined with submission signal scores.
    Query params:
      content_id (str, optional) — filter to one submission
      limit      (int, default 50) — max entries to return
    """
    content_id_filter = request.args.get("content_id")
    limit = min(int(request.args.get("limit", 50)), 200)

    from database import get_audit_log
    raw_entries = get_audit_log(content_id=content_id_filter, limit=limit)

    entries = []
    for r in raw_entries:
        entry = {
            "id":            r["id"],
            "content_id":    r["content_id"],
            "event_type":    r["event_type"],
            "timestamp":     r["timestamp"],
            "status":        r["status"],
        }
        if r["event_type"] == "analysis":
            entry["signals"] = {
                "llm_score":      r.get("llm_score"),
                "stylo_score":    r.get("stylo_score"),
                "signal_gap":     r.get("signal_gap"),
                "composite_score": r.get("composite_score"),
                "confidence_level": r.get("confidence_level"),
                "classification": r.get("classification"),
            }
        elif r["event_type"] == "appeal":
            entry["appeal"] = {
                "appeal_id":             r.get("appeal_id"),
                "reason":                r.get("reason"),
                "previous_classification": r.get("previous_classification"),
                "previous_confidence":   r.get("previous_confidence"),
            }
        entries.append(entry)

    return jsonify({"entries": entries, "total": len(entries)}), 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, port=5000)

