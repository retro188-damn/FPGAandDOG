"""Rule-based parser: noisy Chinese ASR text -> one structured robot command.

Pure functions, no I/O, no model, no regex compilation at call time, so this can
be unit-tested exhaustively and later ported to the board unchanged.

The input is what the ASR actually emits: no punctuation, no sentence
boundaries, filler words, optional spaces, Arabic or Chinese numerals::

    去3号位置并查看板子上的数字
    去 三 号 位 置
    嗯那个请你去第 4 个位置看一下数字

Result schema (always all five keys, same order)::

    {"intent": "goto",    "target": 3,   "actions": ["read_digits"], "raw": "...", "confidence": 0.9}
    {"intent": "stop",    "target": None, "actions": [],             "raw": "...", "confidence": 0.9}
    {"intent": "stand",   "target": None, "actions": [],             "raw": "...", "confidence": 0.9}
    {"intent": "lie",     "target": None, "actions": [],             "raw": "...", "confidence": 0.9}
    {"intent": "unknown", "target": None, "actions": [],             "raw": "...", "confidence": 0.0}

Pipeline: normalise (NFKC -> lowercase -> strip punctuation/spaces -> strip
fillers) -> find movement verb + target number -> find stop/stand/lie keywords
-> pick the most specific intent (goto > stop > stand/lie) and lower the
confidence when more than one intent matched.

Confidence values are hand-tuned, not calibrated probabilities:
0.9 = clean single-intent match, 0.8 = target found without an explicit target
phrase (bare number), 0.6 = competing intents detected, 0.0 = no idea.
"""
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

#: "去 N 号" now means "go to the card showing N" (2026-11-01), so every digit
#: on a card is a valid target -- 0 and 9 included.
VALID_TARGET_MIN = 0
VALID_TARGET_MAX = 9

#: A card can show any digit, so "find the 0" is as valid as "find the 9".
VALID_DIGIT_MIN = 0
VALID_DIGIT_MAX = 9

CONF_CLEAN = 0.9
CONF_WEAK = 0.8
CONF_AMBIGUOUS = 0.6
CONF_NONE = 0.0

# -- normalisation ---------------------------------------------------------
_PUNCT_RE = re.compile(r"[\s，。、！？；：,.!?;:~～…·\-—_/\\|“”‘’\"'()（）\[\]【】《》<>]+")
_FILLER_RE = re.compile(r"那个|嗯+|呃+|额+|啊+|呢+|哦+|哎+|唉+")

# -- vocabulary ------------------------------------------------------------
#: Movement verbs. `到` is guarded so that 看到 / 说到 / 感到 / 得到 do not count.
_VERB_RE = re.compile(r"带我去|走到|前往|过去|(?<![看说感得找遇想])到|去")

#: Stop commands; the single 停 must not fire inside 停车场.
_STOP_RE = re.compile(r"急停|停下|停止|别动|不要动|站住|停(?!车)")
_STAND_WORDS = ("站起来", "起立", "起身", "站立")  # type: Tuple[str, ...]
_LIE_WORDS = ("趴下", "卧倒", "趴着")  # type: Tuple[str, ...]

#: Asking for the card number as well ("go there AND read the digits").
_READ_WORDS = ("数字", "号码", "标号", "识别", "读数", "看", "读", "查", "念", "多少")
#: ... unless it is negated: 不看 / 别读 / 不用识别.
_READ_NEGATION_RE = re.compile(r"(?:不要|不用|别|不)(?:要|用)?(?:看|读|识别|查|念)")

#: A card-read command without any movement ("看看是什么数字").  Deliberately
#: narrower than _READ_WORDS: "多少钱" or "随便看看" must stay unknown, so a
#: bare 看/读/多少 is not enough - the sentence has to name the object it wants
#: read, or ask "what is it".
_READ_OBJECT_WORDS = ("数字", "号码", "标号", "编号", "读数")
_READ_QUESTION_RE = re.compile(r"(?:是|有)多少|是几|写了什么|写的什么|什么数")

#: Sweep-and-find: turn through a full circle and stop on the asked-for digit.
#: Checked *before* "goto", because a bare numeral in "找数字八" would otherwise
#: be read as a station number and the dog would walk instead of turning.
_SEEK_RE = re.compile(
    r"找(?:一下|一找|找)?(?:数字|号码|标号|编号)"
    r"|寻找(?:数字|号码)"
    r"|转(?:一|个|上)?圈"
    r"|扫(?:一|个)?圈"
)

# -- numbers ---------------------------------------------------------------
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}  # type: Dict[str, int]
_NUM_RE = "[0-9零〇一二三四五六七八九十两]+"

#: Target phrases, strongest first.  (compiled pattern, is_explicit_phrase)
_TARGET_PATTERNS = (
    # 第N个位置 / 第N号位 / 第N个点
    (re.compile(r"第\s*(" + _NUM_RE + r")\s*(?:个|号)?\s*(?:位置|位|点|标号|编号)"), True),
    # 第N个 (no location noun)
    (re.compile(r"第\s*(" + _NUM_RE + r")\s*个"), True),
    # N号位置 / N号点 / 三号位
    (re.compile(r"(" + _NUM_RE + r")\s*号"), True),
    # 位置N / 标号N / 编号N / 号码N / 点N
    (re.compile(r"(?:位置|标号|编号|号码|点)\s*(" + _NUM_RE + r")"), True),
    # bare number, weakest (only reached when no phrase above matched)
    (re.compile(r"(" + _NUM_RE + r")"), False),
)  # type: Tuple[Tuple[Any, bool], ...]

#: Characters that turn a bare numeral into a non-numeric phrase: 一下, 一起, 一直,
#: 一圈 (as in "转一圈找数字三", where the 一 must not win over the 三).
_NON_NUMERIC_SUFFIX = "下起样直些共定切旦致同圈"


#: ASR confusions seen in real logs (2026-10-26, logs/voice_agent_20261026.log).
#: Each entry is tied to the transcript that motivated it, so these stay
#: evidence-based repairs instead of turning the parser into a guesser.
_ASR_FIXES = (
    # "去一号后位置" <- 号 heard as 后
    (re.compile(r"(?<=[0-9零〇一二三四五六七八九十])后(?=位置|位|号|点)"), "号"),
    # "看看是什么书" / "什么数子" <- 数字 heard as 书 / 数子
    (re.compile(r"(什么|这个|那个)(书|数子)(?=$|呢|啊)"), r"\1数字"),
    (re.compile(r"(看|读|查|念)(一下)?(什么)?书(?=$|呢|啊)"), r"\1\2\3数字"),
    # "按看是什么数字" <- 看看 heard as 按看
    (re.compile(r"^按看"), "看看"),
    # "位子" <- 位置
    (re.compile(r"位子"), "位置"),
    # "找数字吧" <- 数字八 heard with the numeral lost (2026-11-01 14:47 log)
    (re.compile(r"(数字|号码|标号)(?:吧|巴)(?=$|呢|啊|吧)"), r"\1八"),
)


def _repair_asr(folded: str) -> str:
    """Undo the ASR slips that would otherwise discard a valid command."""
    for pattern, replacement in _ASR_FIXES:
        folded = pattern.sub(replacement, folded)
    return folded


def _normalize(text: str) -> str:
    """Fold the input to the form the rules expect.

    NFKC turns full-width digits into ASCII; punctuation and spaces are dropped
    because ASR inserts them unpredictably; fillers are dropped because they
    carry no command information; finally the known ASR slips are repaired.
    """
    folded = unicodedata.normalize("NFKC", text).lower()
    folded = _PUNCT_RE.sub("", folded)
    folded = _FILLER_RE.sub("", folded)
    return _repair_asr(_PUNCT_RE.sub("", folded))


def _parse_number(token: str) -> Optional[int]:
    """Parse an Arabic or Chinese numeral token; None if it is not a number.

    Handles 一到九, 两, 十, 十一..九十九.  Values outside 1..8 are returned as
    they are -- range checking is the caller's job, so that `去9号位置` can be
    told apart from `去那个位置`.
    """
    if not token:
        return None
    if token.isdigit():
        try:
            return int(token)
        except ValueError:  # pragma: no cover - isdigit() already guarantees this
            return None
    if "十" in token:
        left, _, right = token.partition("十")
        if left and left not in _CN_DIGITS:
            return None
        if right and right not in _CN_DIGITS:
            return None
        tens = _CN_DIGITS[left] if left else 1
        ones = _CN_DIGITS[right] if right else 0
        return tens * 10 + ones
    if len(token) == 1:
        return _CN_DIGITS.get(token)
    return None


def _is_spurious_bare(text: str, end: int) -> bool:
    """True when a bare numeral is really part of a word such as 一下 / 一起."""
    return end < len(text) and text[end] in _NON_NUMERIC_SUFFIX


def _find_target(text: str) -> Tuple[Optional[int], bool]:
    """Return (number, found_in_explicit_target_phrase), or (None, False)."""
    for pattern, explicit in _TARGET_PATTERNS:
        for match in pattern.finditer(text):
            if not explicit and _is_spurious_bare(text, match.end()):
                continue
            value = _parse_number(match.group(1))
            if value is not None:
                return value, explicit
    return None, False


def _first_index(text: str, words: Tuple[str, ...]) -> Optional[int]:
    """Index of the earliest occurrence among `words`, or None if absent."""
    found = None  # type: Optional[int]
    for word in words:
        position = text.find(word)
        if position >= 0 and (found is None or position < found):
            found = position
    return found


def _wants_read(text: str) -> bool:
    """True when the instruction also asks to read what is printed on the card."""
    if not any(word in text for word in _READ_WORDS):
        return False
    return _READ_NEGATION_RE.search(text) is None


def _result(
    intent: str,
    target: Optional[int],
    actions: List[str],
    raw: str,
    confidence: float,
) -> Dict[str, Any]:
    """Build the response dict, keeping the key order stable for JSON output."""
    return {
        "intent": intent,
        "target": target,
        "actions": list(actions),
        "raw": raw,
        "confidence": round(float(confidence), 2),
    }


def parse_intent(text: str) -> Dict[str, Any]:
    """Parse one ASR transcript into the command dict documented above.

    Never raises for plain text: an empty, unrelated or unparsable sentence
    yields ``intent="unknown"`` with confidence 0.0.
    """
    raw = "" if text is None else str(text)
    normalized = _normalize(raw)

    has_verb = _VERB_RE.search(normalized) is not None
    target, explicit = _find_target(normalized)
    target_ok = target is not None and VALID_TARGET_MIN <= target <= VALID_TARGET_MAX

    has_stop = _STOP_RE.search(normalized) is not None
    stand_at = _first_index(normalized, _STAND_WORDS)
    lie_at = _first_index(normalized, _LIE_WORDS)
    has_posture = stand_at is not None or lie_at is not None

    # "转一圈找数字八" is checked before "goto" on purpose: the numeral names the
    # card to look for, so walking to it as a station number would be wrong.
    if (_SEEK_RE.search(normalized) is not None and target is not None
            and VALID_DIGIT_MIN <= target <= VALID_DIGIT_MAX and not has_stop):
        confidence = CONF_AMBIGUOUS if has_posture else CONF_CLEAN
        return _result("seek", int(target), ["read_digits"], raw, confidence)

    if has_verb and target_ok:
        actions = ["read_digits"] if _wants_read(normalized) else []
        confidence = CONF_CLEAN if explicit else CONF_WEAK
        if has_stop or has_posture:
            confidence = min(confidence, CONF_AMBIGUOUS)
        return _result("goto", int(target), actions, raw, confidence)

    if has_stop:
        confidence = CONF_AMBIGUOUS if has_posture else CONF_CLEAN
        return _result("stop", None, [], raw, confidence)

    if has_posture:
        # Both postures present ("站起来别趴着"): the first one spoken wins.
        lie_first = lie_at is not None and (stand_at is None or lie_at < stand_at)
        intent = "lie" if lie_first else "stand"
        confidence = CONF_AMBIGUOUS if (stand_at is not None and lie_at is not None) else CONF_CLEAN
        return _result(intent, None, [], raw, confidence)

    if _wants_read_only(normalized):
        return _result("read", None, ["read_digits"], raw, CONF_CLEAN)

    # A movement verb without a usable 1..8 target is explicitly "I did not
    # understand", not a guess: the dog must not walk anywhere on doubt.
    return _result("unknown", None, [], raw, CONF_NONE)


def _wants_read_only(normalized: str) -> bool:
    """True for "看看是什么数字" / "读一下号码" style commands (no movement)."""
    if _READ_NEGATION_RE.search(normalized):
        return False
    if any(word in normalized for word in _READ_OBJECT_WORDS):
        return True
    return _READ_QUESTION_RE.search(normalized) is not None
