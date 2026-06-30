"""
test_signal1.py — Standalone test for run_llm_signal()

Run with:  .venv/bin/python test_signal1.py

Tests (from spec §M3 Verification):
  1. Known AI-generated paragraph  → expect score >= 0.65
  2. Clearly human passage          → expect score <= 0.45
  3. Empty string / garbage         → expect score == 0.5, fallback == True
  4. Check raw response has 'score' + 'reasoning' keys
"""

import sys
import textwrap

sys.path.insert(0, ".")   # so pipeline is importable from project root
from pipeline import run_llm_signal

RESET  = "\033[0m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"

def check(label: str, result: dict, condition: bool, note: str = ""):
    icon = f"{GREEN}✅{RESET}" if condition else f"{RED}❌{RESET}"
    score_str = f"score={result['llm_score']:.4f}  fallback={result['fallback']}"
    print(f"\n{icon} {BOLD}{label}{RESET}")
    print(f"   {score_str}")
    if result["reasoning"]:
        print(f"   reasoning: {textwrap.shorten(result['reasoning'], 120)}")
    if note:
        print(f"   {YELLOW}note:{RESET} {note}")
    if not condition:
        print(f"   {RED}FAIL: expectation not met{RESET}")

# ---------------------------------------------------------------------------
print(f"\n{BOLD}=== Signal 1 — Groq LLM Tests ==={RESET}\n")

# Test 1 — AI-generated paragraph (expects score >= 0.65)
AI_TEXT = """\
Furthermore, it is important to note that artificial intelligence has revolutionized numerous sectors
of modern society. Moreover, the implications of these technological advancements cannot be overstated.
In conclusion, it is worth considering how these developments will shape the future landscape of human
endeavors. Additionally, one must delve into the nuanced interplay between innovation and societal
adaptation. The synergistic relationship between these elements underscores the importance of
proactive engagement with emerging technologies."""

result1 = run_llm_signal(AI_TEXT)
check("Test 1 — AI paragraph", result1, result1["llm_score"] >= 0.65,
      "expect >= 0.65 (AI-typical transitions, no personal voice)")

# Test 2 — Human-written passage (personal anecdote, idiosyncratic phrasing, expects score <= 0.45)
HUMAN_TEXT = """\
I burned the garlic again. Third time this week — I don't even like garlic that much, but my roommate
does and it's the least I can do after borrowing her car for three weeks without asking. The kitchen
smells like a vampire repellent convention and I've started keeping the windows open even though it's
cold. She hasn't said anything. That's somehow worse."""

result2 = run_llm_signal(HUMAN_TEXT)
check("Test 2 — Human anecdote", result2, result2["llm_score"] <= 0.45,
      "expect <= 0.45 (personal voice, imperfect, idiosyncratic)")

# Test 3 — Empty string (expects fallback=True, score=0.5)
result3 = run_llm_signal("")
check("Test 3 — Empty string", result3,
      result3["fallback"] is True and result3["llm_score"] == 0.5,
      "expect fallback=True, score=0.5, no exception raised")

# Test 4 — Garbage input (expects no exception, fallback safe)
result4 = run_llm_signal("xkcd 327; DROP TABLE students;--   %%%@@@!!!")
check("Test 4 — Garbage input", result4,
      isinstance(result4["llm_score"], float) and 0.0 <= result4["llm_score"] <= 1.0,
      "expect score in [0,1], no crash")

# Test 5 — Response structure check (all keys present)
required_keys = {"llm_score", "reasoning", "fallback"}
has_all_keys = required_keys.issubset(result1.keys())
check("Test 5 — Response structure (keys)", result1, has_all_keys,
      f"expect keys: {required_keys}")

# Summary
print(f"\n{BOLD}=== Done ==={RESET}")
print("If any ❌ appear, inspect the reasoning field — the model may be")
print("scoring differently than the heuristic thresholds in the spec.")
print("The thresholds (>= 0.65 / <= 0.45) are guidelines, not hard constraints.")
