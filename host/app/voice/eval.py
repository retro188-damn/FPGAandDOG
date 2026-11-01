"""Text-level evaluation helpers for the speech pipeline.

Kept separate from the CLI so the maths can be unit-tested: a measurement that
scores the recogniser must itself be correct, otherwise the number in the
report is just decoration.

Two metrics are computed:

* **CER** (character error rate) - Levenshtein distance over characters,
  divided by the length of the reference.  Chinese has no word boundaries, so
  characters are the honest unit here.
* **intent agreement** - does the transcript produce the same (intent, target)
  as the reference sentence?  A recogniser can be 10% CER and still route a
  command to the wrong station, which is the failure that matters for the robot.

Text is normalised before either metric: whitespace and punctuation are
dropped, so "去一号位置。" and "去 一号位置" compare equal.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: Punctuation that carries no meaning for the intent parser.
_PUNCT = "，。、；：？！…—·「」『』（）()《》〈〉,.;:?!\"'`~ \t\r\n"

#: Fillers the recogniser adds when it is unsure; they never change intent but
#: they do inflate CER, so they are reported separately instead of being
#: silently stripped (that would flatter the number).
FILLERS = ("嗯", "呃", "啊", "哦")


def normalize_text(text: Optional[str]) -> str:
    """Drop punctuation and whitespace; keep the characters that carry meaning."""
    if not text:
        return ""
    out = []
    for ch in text:
        if ch in _PUNCT:
            continue
        out.append(ch)
    return "".join(out)


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two strings (insert/delete/substitute = 1)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def cer(reference: Optional[str], hypothesis: Optional[str]) -> float:
    """Character error rate.

    An empty reference means "nothing should have been heard": the score is
    0.0 for an empty hypothesis and 1.0 for any text (a hallucination).
    """
    ref = normalize_text(reference)
    hyp = normalize_text(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / float(len(ref))


def filler_ratio(text: Optional[str]) -> float:
    """Share of characters that are hesitation fillers (0.0 for empty text)."""
    norm = normalize_text(text)
    if not norm:
        return 0.0
    return sum(norm.count(f) for f in FILLERS) / float(len(norm))


def score_entry(entry: Dict[str, object], hypothesis: str,
                intent: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """Score one clip: CER, fillers, and whether the routing survived."""
    expected = str(entry.get("expected") or "")
    want_intent = entry.get("intent")
    got_intent = (intent or {}).get("intent")
    want_target = entry.get("target")
    got_target = (intent or {}).get("target")
    intent_ok = None
    if want_intent is not None:
        intent_ok = (got_intent == want_intent) and (want_target == got_target)
    return {
        "file": entry.get("file"),
        "expected": expected,
        "hypothesis": hypothesis,
        "cer": round(cer(expected, hypothesis), 4),
        "fillers": round(filler_ratio(hypothesis), 4),
        "intent_expected": want_intent,
        "intent_got": got_intent,
        "target_expected": want_target,
        "target_got": got_target,
        "intent_ok": intent_ok,
    }


def summarize(rows: Sequence[Dict[str, object]],
              aggregate: Iterable[str] = ()) -> Dict[str, object]:
    """Aggregate scored rows.

    ``aggregate`` restricts the mean CER to the named files when the caller
    wants to exclude a known-broken capture from the headline number (the
    excluded rows are still printed so nothing is hidden).
    """
    keep = set(aggregate) if aggregate else None
    used = [r for r in rows if keep is None or r["file"] in keep]
    if not used:
        return {"count": 0}
    cers = [float(r["cer"]) for r in used]
    judged = [r for r in used if r["intent_ok"] is not None]
    hallucinations = [
        r for r in used if not str(r.get("expected") or "") and r.get("hypothesis")
    ]
    return {
        "count": len(used),
        "mean_cer": round(sum(cers) / len(cers), 4),
        "worst_cer": round(max(cers), 4),
        "intent_checked": len(judged),
        "intent_ok": sum(1 for r in judged if r["intent_ok"]),
        "hallucinations_on_silence": len(hallucinations),
    }


__all__: List[str] = [
    "FILLERS",
    "cer",
    "edit_distance",
    "filler_ratio",
    "normalize_text",
    "score_entry",
    "summarize",
]
