"""The progress log's table of contents must not lose a section by accident.

2026-10-28: an edit spliced new text over the ``## 2026-10-27`` heading and the
whole entry silently became part of the 10-28 section.  Nothing failed, the file
still looked plausible, and only a human noticed.  This test pins the heading
list: adding a section is a deliberate two-line change, losing one is a red test.
"""

import io
from pathlib import Path

HEADINGS = [
    '## 2026-10-29（周四·凌晨）：离线换算法——先修"看不见"，再修"认不出"',
    '## 2026-10-28（周三·深夜）：转身不再漂、会低头了、以及"8 只有 200 px 才读对"',
    '## 2026-10-28（周三·夜）：真机"转一圈找指定数字"跑通',
    '## 2026-10-28（周三）：把"读错"从置信度问题变成质量问题（停稳再拍）',
    '## 2026-10-27（周二）：上板验收三项全过 + 狗会自己找卡',
    '## 长期待办（用户 2026-10-26 提出，按优先级待排）',
    '## 2026-10-26（第 2 天）',
    '## 夜间无人值守：2026-10-26 22:00 → 23:55（狗/板子全部断电）',
]

LOG = Path(__file__).resolve().parent.parent / "docs" / "progress.md"


def _headings():
    text = io.open(LOG, encoding="utf-8").read()
    return [line for line in text.splitlines() if line.startswith("## ")]


def test_every_section_heading_is_still_there():
    assert _headings() == HEADINGS


def test_sections_are_not_duplicated():
    seen = _headings()
    assert len(seen) == len(set(seen)), "a section heading appears twice"
