"""Turn a parsed intent into an executable task plan and the words to say.

The hardware half (dog motion, digit reading) is not attached yet, so a plan is
split into:

* `steps`  - the ordered work items a future executor will consume
             (`SPEAK`, `MOVE`, `READ_DIGITS`, `STOP`, `STAND`, `LIE`);
* `say`    - the utterances in order, so the dialogue half is demonstrable
             today without the dog or the FPGA.

Keeping the two lists separate is deliberate: the TTS layer can play `say`
immediately for a demo, while the executor later walks `steps` and inserts the
real digit into the final sentence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CN_DIGITS = "零一二三四五六七八九十"


def cn_number(value: int) -> str:
    """1..99 -> Chinese numerals, good enough for board positions."""
    if value < 0 or value > 99:
        return str(value)
    if value <= 10:
        return CN_DIGITS[value]
    tens, ones = divmod(value, 10)
    head = "十" if tens == 1 else CN_DIGITS[tens] + "十"
    return head + (CN_DIGITS[ones] if ones else "")


@dataclass
class Step:
    """One unit of work for the executor (hardware not wired yet)."""

    kind: str
    target: Optional[int] = None
    text: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.kind}
        if self.target is not None:
            out["target"] = self.target
        if self.text:
            out["text"] = self.text
        if self.payload:
            out["payload"] = self.payload
        return out


@dataclass
class TaskPlan:
    intent: str
    target: Optional[int]
    actions: List[str]
    say: List[str]
    steps: List[Step]
    requires_hardware: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "target": self.target,
            "actions": self.actions,
            "say": self.say,
            "steps": [step.to_dict() for step in self.steps],
            "requires_hardware": self.requires_hardware,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def plan_from_intent(intent: Dict[str, Any]) -> TaskPlan:
    """Build the plan for one parsed intent (never raises)."""
    kind = str(intent.get("intent") or "unknown")
    target = intent.get("target")
    actions = list(intent.get("actions") or [])

    if kind == "goto" and isinstance(target, int):
        return _goto_plan(target, actions)
    if kind == "stop":
        return TaskPlan(
            intent=kind,
            target=None,
            actions=actions or ["stop"],
            say=["好的，停下"],
            steps=[Step(kind="STOP"), Step(kind="SPEAK", text="好的，停下")],
            requires_hardware=True,
        )
    if kind == "read":
        return _read_plan()
    if kind in ("stand", "stand_up"):
        return TaskPlan(
            intent="stand",
            target=None,
            actions=actions or ["stand"],
            say=["收到，起立"],
            steps=[Step(kind="STAND"), Step(kind="SPEAK", text="收到，起立")],
            requires_hardware=True,
        )
    if kind in ("lie", "lie_down"):
        return TaskPlan(
            intent="lie",
            target=None,
            actions=actions or ["lie"],
            say=["收到，趴下"],
            steps=[Step(kind="LIE"), Step(kind="SPEAK", text="收到，趴下")],
            requires_hardware=True,
        )
    return TaskPlan(
        intent="unknown",
        target=None,
        actions=[],
        say=["没听清，请再说一遍"],
        steps=[Step(kind="SPEAK", text="没听清，请再说一遍")],
        requires_hardware=False,
    )


def _goto_plan(target: int, actions: List[str]) -> TaskPlan:
    """Walk to position N, optionally read the digits shown there."""
    place = "{}号位置".format(cn_number(target))
    read_digits = "read_digits" in actions
    say = ["收到，前往" + place]
    if read_digits:
        say.append("正在查看" + place)
    steps = [
        Step(kind="MOVE", target=target, payload={"station": target}),
        Step(kind="SPEAK", text=say[0]),
    ]
    if read_digits:
        steps.append(Step(kind="READ_DIGITS", target=target))
        steps.append(Step(kind="SPEAK", text="", payload={"slot": "digit_result"}))
    return TaskPlan(
        intent="goto",
        target=target,
        actions=actions or (["read_digits"] if read_digits else []),
        say=say,
        steps=steps,
        requires_hardware=True,
    )


def _read_plan() -> TaskPlan:
    """Read the card where the dog is standing right now (no movement)."""
    say = ["让我看看"]
    return TaskPlan(
        intent="read",
        target=None,
        actions=["read_digits"],
        say=say,
        steps=[
            Step(kind="READ_DIGITS", target=None),
            Step(kind="SPEAK", text="", payload={"slot": "digit_result"}),
        ],
        requires_hardware=False,
    )


def digit_sentence(digits: Optional[str]) -> str:
    """Final spoken line once the board has been read (digits may be None)."""
    if not digits:
        return "没看清板子上的数字"
    digits = str(digits).strip()
    if all(ch in "0123456789" for ch in digits):
        return "板子上写的是" + "".join(CN_DIGITS[int(ch)] for ch in digits)
    return "板子上写的是" + digits
