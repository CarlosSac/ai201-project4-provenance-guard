"""
app.py — Provenance Guard Flask application

Routes:
  POST /api/analyze  (alias: /submit)  — run both signals, aggregate, return scored response
  POST /api/appeal   (alias: /appeal)  — creator appeal workflow
  GET  /api/log      (alias: /log)     — audit log viewer

M4: Signal 2 (stylometrics) + confidence scoring live.
M5: Transparency labels, appeals workflow, rate limiting, full audit log live.
"""

import logging
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from database import (
    init_db, insert_submission, insert_audit_event, get_audit_log,
    get_submission, update_submission_status,
)
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
@app.route("/submit", methods=["POST"])        # alias used in rate-limit tests
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
# POST /api/appeal  (also aliased as POST /appeal for curl convenience)
# ---------------------------------------------------------------------------
@app.route("/api/appeal", methods=["POST"])
@app.route("/appeal", methods=["POST"])
@limiter.limit("5 per minute")
def appeal():
    """
    Submit a creator appeal against a classification.

    Required JSON fields:
      content_id        (str) — UUID from the original /api/analyze response
      reason            (str) — free-text explanation (min 10 chars)
                                also accepted as 'creator_reasoning'

    Responses:
      200  — appeal accepted, status set to under_review
      400  — missing/invalid fields
      404  — content_id not found
      409  — already under review (no duplicate appeals)
      429  — rate limit exceeded
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    content_id = data.get("content_id", "").strip()
    if not content_id:
        return jsonify({"error": "Missing required field: content_id"}), 400

    # Accept both field names so the milestone curl example works verbatim
    reason = (
        data.get("reason", "")
        or data.get("creator_reasoning", "")
    ).strip()
    if len(reason) < 10:
        return jsonify({
            "error": "Field 'reason' (or 'creator_reasoning') is required and must be at least 10 characters"
        }), 400

    # ── Look up the submission ───────────────────────────────────────────────
    submission = get_submission(content_id)
    if submission is None:
        return jsonify({"error": f"content_id '{content_id}' not found"}), 404

    # ── Prevent duplicate appeals ────────────────────────────────────────────
    if submission["status"] == "under_review":
        return jsonify({
            "error": "An appeal for this content is already under review",
            "content_id": content_id,
            "status": "under_review",
        }), 409

    # ── Update submission status ─────────────────────────────────────────────
    update_submission_status(content_id, "under_review")

    # ── Log the appeal event ─────────────────────────────────────────────────
    appeal_id = str(uuid.uuid4())
    now_iso   = datetime.now(timezone.utc).isoformat()

    insert_audit_event({
        "content_id":              content_id,
        "event_type":              "appeal",
        "appeal_id":               appeal_id,
        "reason":                  reason,
        "previous_classification": submission["classification"],
        "previous_confidence":     submission["confidence_score"],
        "status":                  "under_review",
        "timestamp":               now_iso,
    })

    logger.info(
        "Appeal submitted appeal_id=%s  content_id=%s  previous=%s",
        appeal_id, content_id, submission["classification"],
    )

    # ── Response (spec §4) ───────────────────────────────────────────────────
    return jsonify({
        "appeal_id":   appeal_id,
        "content_id":  content_id,
        "status":      "under_review",
        "message": (
            "Appeal submitted. Your content has been flagged for human review. "
            "The label may be updated once a reviewer has assessed your appeal."
        ),
        "timestamp":   now_iso,
    }), 200


# ---------------------------------------------------------------------------
# GET /api/log  (also aliased as GET /log for checkpoint curl convenience)
# ---------------------------------------------------------------------------
@app.route("/api/log", methods=["GET"])
@app.route("/log", methods=["GET"])
@limiter.limit("30 per minute")
def log():
    """
    Return audit log entries as JSON.
    Query params:
      content_id (str, optional) — filter to one submission
      limit      (int, default 50) — max entries to return
    """
    content_id_filter = request.args.get("content_id")
    limit = min(int(request.args.get("limit", 50)), 200)

    raw_entries = get_audit_log(content_id=content_id_filter, limit=limit)

    entries = []
    for r in raw_entries:
        entry = {
            "id":          r["id"],
            "content_id":  r["content_id"],
            "event_type":  r["event_type"],
            "timestamp":   r["timestamp"],
            "status":      r["status"],
        }
        if r["event_type"] == "analysis":
            entry["signals"] = {
                "llm_score":        r.get("llm_score"),
                "stylo_score":      r.get("stylo_score"),
                "signal_gap":       r.get("signal_gap"),
                "composite_score":  r.get("composite_score"),
                "confidence_level": r.get("confidence_level"),
                "classification":   r.get("classification"),
            }
        elif r["event_type"] == "appeal":
            entry["appeal"] = {
                "appeal_id":               r.get("appeal_id"),
                # Expose under both names so log consumers see it either way
                "reason":                  r.get("reason"),
                "appeal_reasoning":        r.get("reason"),
                "previous_classification": r.get("previous_classification"),
                "previous_confidence":     r.get("previous_confidence"),
            }
        entries.append(entry)

    return jsonify({"entries": entries, "total": len(entries)}), 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, port=5002, host="127.0.0.1")

