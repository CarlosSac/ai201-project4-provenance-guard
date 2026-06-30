"""
pipeline.py — Detection signal functions for Provenance Guard

Signal 1: run_llm_signal(text) — Groq LLM (llama-3.3-70b-versatile)
  Returns: llm_score (float 0.0–1.0), reasoning (str)
  Fallback: returns 0.5 on any parse/API failure, logs the error

Signal 2: run_stylo_signal(text) — Stylometric heuristics (pure Python)
  Computes sentence length variance and type-token ratio, normalises each
  to [0, 1] per the spec formulas, and returns stylo_score as a float.

Aggregation: aggregate_signals(text, llm_score, stylo_score)
  Combines both signals using weighted formula (0.6 LLM + 0.4 stylo),
  applies signal-gap check, and returns composite_score, confidence_level,
  and classification per the planning.md threshold table.
"""

import json
import os
import re
import logging
import statistics
import re as _re

from groq import Groq
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Groq client (module-level singleton — one client, reused across calls)
# ---------------------------------------------------------------------------
_groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# ---------------------------------------------------------------------------
# Prompt template — spec §1 Signal 1
# ---------------------------------------------------------------------------
_LLM_PROMPT_TEMPLATE = """\
You are an AI authorship detection expert. Analyze the following text and return a JSON object with:
- "score": a float from 0.0 (clearly human-written) to 1.0 (clearly AI-generated)
- "reasoning": a quoted string — one sentence explaining your assessment

IMPORTANT: All JSON string values must be wrapped in double quotes.

Focus on: predictable phrasing, uniform sentence rhythm, AI-typical transitions, absence of personal voice.
Do not factor in topic or subject matter — only writing style and structure.

TEXT:
{content}

Respond with only valid JSON. No explanation outside the JSON object.
Example of the required format:
{{"score": 0.85, "reasoning": "The text uses repetitive transitional phrases and has no personal voice."}}"""


def _repair_unquoted_reasoning(raw: str) -> str:
    """
    llama-3.3-70b-versatile occasionally emits the 'reasoning' value without
    surrounding double-quotes, producing syntactically invalid JSON:

      {"score": 0.9, "reasoning": The text is ... .}

    This function detects that pattern and wraps the value in double-quotes
    so json.loads can parse it. Only applied after vanilla json.loads fails.
    """
    # Match:  "reasoning": <unquoted value up to the closing brace>
    pattern = re.compile(
        r'("reasoning"\s*:\s*)([^"\[\{][^\n}]*?)(\s*\})',
        re.DOTALL,
    )
    def _quote(m: re.Match) -> str:
        key   = m.group(1)
        value = m.group(2).strip().rstrip(",").replace('"', '\\"')
        close = m.group(3)
        return f'{key}"{value}"{close}'

    return pattern.sub(_quote, raw, count=1)


def _extract_json(raw: str) -> dict:
    """
    Pull the first {...} block out of the model's raw response and return a
    parsed dict.  Handles four common failure modes from llama-3.3-70b:
      1. Direct valid JSON (ideal)
      2. JSON wrapped in a markdown code fence
      3. Unquoted string value for 'reasoning'
      4. Any {...} block buried in surrounding prose

    Raises ValueError if nothing parseable is found.
    """
    text = raw.strip()

    # ① Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # ② Strip code fence and retry
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence_match:
        inner = fence_match.group(1).strip()
        # ③ Try unquoted-reasoning repair on the fenced content
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            repaired = _repair_unquoted_reasoning(inner)
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    # ④ Grab any {...} block and try repair
    brace_match = re.search(r"\{[\s\S]*?\}", text)
    if brace_match:
        block = brace_match.group(0)
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            repaired = _repair_unquoted_reasoning(block)
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    raise ValueError(f"No parseable JSON object found in model output: {raw!r}")


# ---------------------------------------------------------------------------
# Signal 1 — LLM Classifier
# ---------------------------------------------------------------------------
def run_llm_signal(text: str) -> dict:
    """
    Send `text` to Groq llama-3.3-70b-versatile and return:
      {
        "llm_score":  float,   # 0.0 (human) → 1.0 (AI); 0.5 on fallback
        "reasoning":  str,     # one-sentence model explanation
        "fallback":   bool,    # True if the 0.5 fallback was triggered
      }

    Never raises — callers can always trust the return dict.
    """
    if not text or not text.strip():
        logger.warning("run_llm_signal called with empty text; returning fallback 0.5")
        return {"llm_score": 0.5, "reasoning": "Empty input", "fallback": True}

    prompt = _LLM_PROMPT_TEMPLATE.format(content=text)

    try:
        chat_completion = _groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.0,          # deterministic scoring
            max_tokens=256,           # score + one sentence is well under 256
        )

        raw_content = chat_completion.choices[0].message.content
        logger.debug("Groq raw response: %s", raw_content)

        parsed = _extract_json(raw_content)

        # Validate score is a numeric in [0, 1]
        score = parsed.get("score")
        if not isinstance(score, (int, float)):
            raise ValueError(f"'score' is not numeric: {score!r}")
        score = float(score)
        if not (0.0 <= score <= 1.0):
            raise ValueError(f"'score' out of range [0,1]: {score}")

        reasoning = str(parsed.get("reasoning", "")).strip()

        return {"llm_score": round(score, 4), "reasoning": reasoning, "fallback": False}

    except Exception as exc:  # noqa: BLE001
        logger.error("run_llm_signal failed (%s: %s); returning fallback 0.5", type(exc).__name__, exc)
        return {"llm_score": 0.5, "reasoning": f"Fallback triggered: {exc}", "fallback": True}


# ---------------------------------------------------------------------------
# Signal 2 — Stylometric Heuristics
# ---------------------------------------------------------------------------

# Simple sentence splitter: split on ., !, ? followed by whitespace or end-of-string
_SENTENCE_SPLIT = _re.compile(r'(?<=[.!?])\s+')


def _split_sentences(text: str) -> list[str]:
    """Split text into non-empty sentences."""
    parts = _SENTENCE_SPLIT.split(text.strip())
    return [s.strip() for s in parts if s.strip()]


def _tokenize_words(text: str) -> list[str]:
    """Return lowercase alpha tokens (strips punctuation)."""
    return _re.findall(r"[a-z']+", text.lower())


def run_stylo_signal(text: str) -> dict:
    """
    Compute stylometric features and return:
      {
        "stylo_score":   float,   # 0.0 (human) → 1.0 (AI)
        "slv_score":     float,   # sentence-length-variance sub-score
        "ttr_score":     float,   # type-token-ratio sub-score
        "variance":      float,   # raw sentence-length variance
        "ttr":           float,   # raw type-token ratio
        "stylo_reliable": bool,   # False when text < 50 words
      }

    Formulas (verbatim from planning.md §1 Signal 2):
      slv_score = max(0, min(1, 1.0 - variance / 50.0))
        where variance = statistics.variance(sentence_lengths)
        AI signature: low variance → slv_score near 1.0

      ttr_score = 1.0 - min(abs(ttr - 0.5) / 0.2, 1.0)
        where ttr = unique_words / total_words
        AI signature: ttr near 0.5 → ttr_score near 1.0

      stylo_score = (slv_score + ttr_score) / 2.0
    """
    words = _tokenize_words(text)
    word_count = len(words)
    stylo_reliable = word_count >= 50

    sentences = _split_sentences(text)

    # --- Sentence Length Variance (SLV) ---
    if len(sentences) > 1:
        lengths = [len(s.split()) for s in sentences]
        variance = statistics.variance(lengths)
    else:
        variance = 0.0  # single sentence → zero variance → AI-leaning

    slv_score = max(0.0, min(1.0, 1.0 - (variance / 50.0)))

    # --- Type-Token Ratio (TTR) ---
    if words:
        ttr = len(set(words)) / len(words)
    else:
        ttr = 0.0

    ttr_score = 1.0 - min(abs(ttr - 0.5) / 0.2, 1.0)

    # --- Combined stylometric score (equal weight) ---
    stylo_score = (slv_score + ttr_score) / 2.0

    return {
        "stylo_score":    round(stylo_score, 4),
        "slv_score":      round(slv_score, 4),
        "ttr_score":      round(ttr_score, 4),
        "variance":       round(variance, 4),
        "ttr":            round(ttr, 4),
        "stylo_reliable": stylo_reliable,
    }


# ---------------------------------------------------------------------------
# Signal Aggregator — spec §1 Signal Combination + §2 Uncertainty
# ---------------------------------------------------------------------------

# Threshold constants (kept here so tests can import them directly)
SIGNAL_GAP_THRESHOLD    = 0.35   # above this → signals disagree → low confidence
HIGH_AI_THRESHOLD       = 0.75   # composite >= this AND gap ok → ai_generated / high
HIGH_HUMAN_THRESHOLD    = 0.25   # composite <= this AND gap ok → human_written / high
LLM_WEIGHT              = 0.6
STYLO_WEIGHT            = 0.4
MIN_WORDS_FOR_STYLO     = 50


def aggregate_signals(text: str, llm_score: float, stylo_score: float) -> dict:
    """
    Combine LLM and stylometric scores into a composite result.

    Returns:
      {
        "composite_score":   float,   # weighted combination (or llm_score alone)
        "confidence_level":  str,     # 'high' | 'medium' | 'low'
        "classification":    str,     # 'ai_generated' | 'human_written' | 'uncertain'
        "signal_gap":        float | None,
        "stylo_reliable":    bool,
      }

    Exact logic from planning.md §1 Signal Combination:
      stylo_reliable = word_count >= 50

      if stylo_reliable:
          composite = 0.6 * llm_score + 0.4 * stylo_score
          signal_gap = abs(llm_score - stylo_score)
          if signal_gap > 0.35:        → confidence_level = 'low'
          elif composite >= 0.75:      → confidence_level = 'high'
          elif composite <= 0.25:      → confidence_level = 'high'
          else:                        → confidence_level = 'medium'
      else:
          composite = llm_score
          signal_gap = None
          confidence_level = 'medium'

    Classification from §2 threshold table:
      composite >= 0.75 AND gap <= 0.35  → ai_generated
      composite <= 0.25 AND gap <= 0.35  → human_written
      everything else                    → uncertain
    """
    word_count = len(text.split())
    stylo_reliable = word_count >= MIN_WORDS_FOR_STYLO

    if stylo_reliable:
        composite_score = (LLM_WEIGHT * llm_score) + (STYLO_WEIGHT * stylo_score)
        signal_gap = abs(llm_score - stylo_score)

        if signal_gap > SIGNAL_GAP_THRESHOLD:
            confidence_level = "low"
        elif composite_score >= HIGH_AI_THRESHOLD:
            confidence_level = "high"
        elif composite_score <= HIGH_HUMAN_THRESHOLD:
            confidence_level = "high"
        else:
            confidence_level = "medium"
    else:
        composite_score = llm_score
        signal_gap = None
        confidence_level = "medium"

    # --- Classification (§2 threshold table) ---
    # Only assign a directional classification when signals agree AND score is extreme
    gap_ok = (signal_gap is None) or (signal_gap <= SIGNAL_GAP_THRESHOLD)

    if gap_ok and composite_score >= HIGH_AI_THRESHOLD:
        classification = "ai_generated"
    elif gap_ok and composite_score <= HIGH_HUMAN_THRESHOLD:
        classification = "human_written"
    else:
        classification = "uncertain"

    return {
        "composite_score":  round(composite_score, 4),
        "confidence_level": confidence_level,
        "classification":   classification,
        "signal_gap":       round(signal_gap, 4) if signal_gap is not None else None,
        "stylo_reliable":   stylo_reliable,
    }
