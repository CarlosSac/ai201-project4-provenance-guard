"""
test_signal2.py — Standalone tests for Signal 2 (stylometrics) and aggregate_signals.
No network calls. Run with:  python test_signal2.py
"""

from pipeline import run_stylo_signal, aggregate_signals, SIGNAL_GAP_THRESHOLD

# ANSI helpers
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"

PASS = f"{GREEN}PASS{RESET}"
FAIL = f"{RED}FAIL{RESET}"
INFO = f"{YELLOW}INFO{RESET}"


def check(label: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    if detail:
        print(f"         {detail}")
    if not condition:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Test texts
# ---------------------------------------------------------------------------

AI_TEXT = """\
Artificial intelligence has fundamentally transformed the landscape of modern technology.
It is important to note that machine learning algorithms have become increasingly sophisticated.
Moreover, the integration of deep neural networks enables more accurate predictions.
Furthermore, natural language processing capabilities have advanced significantly.
In conclusion, AI represents a pivotal development in human history.
These systems offer remarkable opportunities for improving productivity and efficiency.
Additionally, researchers continue to explore novel applications across diverse domains.
"""

HUMAN_TEXT = """\
I still remember the first time I got completely lost hiking. I was maybe 22, dragged
a heavy pack up a trail that just... stopped. No marker. Sat on a rock for a good
hour eating stale crackers, genuinely unsure if I should go back or push forward.
Chose forward. Found a ridge, then a meadow, then the right path again — total
accident, honestly. Would I do it differently? Probably not.
The point is you learn stuff when the plan falls apart. My compass was busted anyway.
And the crackers were terrible. Why do I keep buying those? They taste like cardboard
with ambitions. Next time I'll bring actual food. Or at least better crackers.
"""

SHORT_TEXT = "This is a short text under fifty words total."

ESL_TEXT = """\
In this essay, I will discuss the importance of environmental conservation.
Firstly, it is well known that deforestation leads to significant climate change.
Furthermore, the loss of biodiversity has many negative consequences for ecosystems.
In addition, pollution of water resources poses serious threats to human health.
Therefore, governments should implement strict regulations to protect the environment.
In conclusion, it is essential that all citizens contribute to conservation efforts.
"""

# ---------------------------------------------------------------------------
# Section 1: run_stylo_signal — basic output shape
# ---------------------------------------------------------------------------
print("\n=== run_stylo_signal: output shape ===")
r = run_stylo_signal(AI_TEXT)
print(f"  AI text raw: {r}")
check("all keys present", all(k in r for k in
      ["stylo_score","slv_score","ttr_score","variance","ttr","stylo_reliable"]))
check("stylo_score in [0,1]", 0.0 <= r["stylo_score"] <= 1.0)
check("stylo_reliable=True for AI_TEXT (>50 words)", r["stylo_reliable"] is True)

# ---------------------------------------------------------------------------
# Section 2: AI text — expects high stylo_score (uniform sentences, AI-band TTR)
# ---------------------------------------------------------------------------
print("\n=== run_stylo_signal on AI text ===")
r_ai = run_stylo_signal(AI_TEXT)
print(f"  stylo_score={r_ai['stylo_score']}  slv={r_ai['slv_score']}  ttr_score={r_ai['ttr_score']}")
print(f"  variance={r_ai['variance']}  ttr={r_ai['ttr']}")
check("AI text stylo_score >= 0.45 (AI-leaning)", r_ai["stylo_score"] >= 0.45,
      f"got {r_ai['stylo_score']}")

# ---------------------------------------------------------------------------
# Section 3: Human text — expects lower stylo_score (more varied sentences)
# ---------------------------------------------------------------------------
print("\n=== run_stylo_signal on human text ===")
r_hum = run_stylo_signal(HUMAN_TEXT)
print(f"  stylo_score={r_hum['stylo_score']}  slv={r_hum['slv_score']}  ttr_score={r_hum['ttr_score']}")
print(f"  variance={r_hum['variance']}  ttr={r_hum['ttr']}")
check("Human text stylo_score < AI text stylo_score (relative)",
      r_hum["stylo_score"] < r_ai["stylo_score"],
      f"human={r_hum['stylo_score']} vs ai={r_ai['stylo_score']}")

# ---------------------------------------------------------------------------
# Section 4: Short text — stylo_reliable must be False
# ---------------------------------------------------------------------------
print("\n=== run_stylo_signal on short text (<50 words) ===")
r_short = run_stylo_signal(SHORT_TEXT)
print(f"  stylo_score={r_short['stylo_score']}  reliable={r_short['stylo_reliable']}")
check("Short text: stylo_reliable=False", r_short["stylo_reliable"] is False)

# ---------------------------------------------------------------------------
# Section 5: ESL academic text — signal interesting to observe
# ---------------------------------------------------------------------------
print("\n=== run_stylo_signal on ESL text ===")
r_esl = run_stylo_signal(ESL_TEXT)
print(f"  stylo_score={r_esl['stylo_score']}  slv={r_esl['slv_score']}  ttr_score={r_esl['ttr_score']}")
print(f"  [INFO] ESL score noted — both signals may agree as AI-leaning")

# ---------------------------------------------------------------------------
# Section 6: aggregate_signals — spec threshold verification
# ---------------------------------------------------------------------------
print("\n=== aggregate_signals: threshold verification ===")

# 6a. High AI: both signals clearly AI, gap small -> ai_generated / high
r = aggregate_signals("word " * 60, llm_score=0.85, stylo_score=0.80)
print(f"  6a high-AI: {r}")
check("6a composite >= 0.75", r["composite_score"] >= 0.75, f"got {r['composite_score']}")
check("6a confidence_level=high", r["confidence_level"] == "high")
check("6a classification=ai_generated", r["classification"] == "ai_generated")
check("6a gap <= 0.35", r["signal_gap"] <= SIGNAL_GAP_THRESHOLD)

# 6b. High Human: both signals clearly human, gap small -> human_written / high
r = aggregate_signals("word " * 60, llm_score=0.15, stylo_score=0.20)
print(f"  6b high-human: {r}")
check("6b composite <= 0.25", r["composite_score"] <= 0.25, f"got {r['composite_score']}")
check("6b confidence_level=high", r["confidence_level"] == "high")
check("6b classification=human_written", r["classification"] == "human_written")

# 6c. Signal disagreement -> low confidence / uncertain
r = aggregate_signals("word " * 60, llm_score=0.80, stylo_score=0.30)
print(f"  6c signal-gap: {r}")
check("6c signal_gap > 0.35", r["signal_gap"] > SIGNAL_GAP_THRESHOLD,
      f"gap={r['signal_gap']}")
check("6c confidence_level=low", r["confidence_level"] == "low")
check("6c classification=uncertain (gap overrides)", r["classification"] == "uncertain")

# 6d. Middle-band composite -> medium confidence / uncertain
r = aggregate_signals("word " * 60, llm_score=0.55, stylo_score=0.50)
print(f"  6d middle-band: {r}")
check("6d composite in (0.25, 0.75)", 0.25 < r["composite_score"] < 0.75)
check("6d confidence_level=medium", r["confidence_level"] == "medium")
check("6d classification=uncertain", r["classification"] == "uncertain")

# 6e. Short text -> stylo bypassed, composite = llm_score, medium confidence
r = aggregate_signals(SHORT_TEXT, llm_score=0.80, stylo_score=0.50)
print(f"  6e short text: {r}")
check("6e stylo_reliable=False", r["stylo_reliable"] is False)
check("6e composite==llm_score", r["composite_score"] == 0.80)
check("6e confidence_level=medium (no second signal)", r["confidence_level"] == "medium")
check("6e signal_gap=None", r["signal_gap"] is None)
check("6e classification=ai_generated (gap_ok=True, composite>=0.75)",
      r["classification"] == "ai_generated")

print(f"\nAll tests passed.\n")
