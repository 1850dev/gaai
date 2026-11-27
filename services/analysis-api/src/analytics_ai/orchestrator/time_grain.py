"""Bounded interpretation of explicit grouping requests, shared by planning/coverage.

This is not a general language parser. Unresolved alternatives/contradictions are
kept ambiguous; multiple affirmative grains are never reduced to a first match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

from analytics_ai.compat import StrEnum


class TimeGrain(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


GRAIN_FIELDS = {
    TimeGrain.DAY: "date",
    TimeGrain.WEEK: "yearWeek",
    TimeGrain.MONTH: "yearMonth",
    TimeGrain.YEAR: "year",
}

_PATTERNS = (
    (TimeGrain.DAY, r"日別|日ごと|日単位|日次|デイリー|\bdaily\b|\b(?:by|per)\s+day\b"),
    (
        TimeGrain.WEEK,
        r"週別|週ごと|週単位|週次|ウィークリー|\bweekly\b|\b(?:by|per)\s+week\b",
    ),
    (
        TimeGrain.MONTH,
        r"月別|月ごと|月単位|月次|月度|マンスリー|\bmonthly\b|\b(?:by|per)\s+month\b",
    ),
    (
        TimeGrain.YEAR,
        r"年別|年ごと|年単位|年次|イヤリー|\byearly\b|\bannual(?:ly)?\b|\b(?:by|per)\s+year\b",
    ),
)
# Never inspect another grain's predicate or cross a sentence boundary.
_NEGATIVE_SUFFIX = re.compile(
    r"^\s*(?:の(?:合計|集計|データ|表示|推移|数値|値|結果|UU|ユーザー数|PV))?\s*"
    r"(?:ではなく|でなく|じゃなく|ではない|じゃない|"
    r"(?:は|を|で)?(?:不要|いらない|要らない|除外|やめて|やめる)|"
    r"(?:には?|で(?:は)?)?(?:集計|表示|分析|取得|比較)しない)"
)
_NEGATIVE_PREFIX = re.compile(r"(?:\bnot|\binstead\s+of|\brather\s+than)\s*$", re.I)
_ALTERNATIVE = re.compile(r"(?:または|あるいは|もしくは|\bor\b|か|/)\s*$", re.I)
_UNCERTAIN_SUFFIX = re.compile(
    r"^\s*(?:は)?(?:不要ではない|ではなくない|しないわけではない)"
)


@dataclass(frozen=True)
class GrainRequest:
    requested: tuple[TimeGrain, ...] = ()
    excluded: tuple[TimeGrain, ...] = ()
    mentioned: bool = False
    ambiguous: bool = False

    @property
    def single(self) -> TimeGrain | None:
        return (
            self.requested[0]
            if len(self.requested) == 1 and not self.ambiguous
            else None
        )

    @property
    def needs_clarification(self) -> bool:
        return self.ambiguous or (self.mentioned and not self.requested)


def time_grain_request(question: str) -> GrainRequest:
    mentions = sorted(
        (match.start(), match.end(), grain)
        for grain, pattern in _PATTERNS
        for match in re.finditer(pattern, question, re.I)
    )
    positive: list[TimeGrain] = []
    negative: list[TimeGrain] = []
    positive_spans: list[tuple[int, int]] = []
    uncertain = False
    for index, (start, end, grain) in enumerate(mentions):
        previous = mentions[index - 1][1] if index else 0
        following = (
            mentions[index + 1][0] if index + 1 < len(mentions) else len(question)
        )
        prefix, suffix = question[previous:start], question[end:following]
        uncertain = uncertain or bool(_UNCERTAIN_SUFFIX.search(suffix))
        negated = bool(
            _NEGATIVE_SUFFIX.search(suffix) or _NEGATIVE_PREFIX.search(prefix)
        )
        # Explicit A -> B replacement, not arbitrary "last grain wins".
        if index + 1 < len(mentions):
            target_end = mentions[index + 1][1]
            after_target = question[target_end : target_end + 32]
            if re.fullmatch(r"\s*(?:から|を)\s*", suffix) and re.match(
                r"\s*(?:に|へ)(?:変更|変え|切り替|切替|して|する)", after_target
            ):
                negated = True
            if re.fullmatch(r"\s+to\s+", suffix, re.I) and re.search(
                r"\bfrom\s*$", prefix, re.I
            ):
                negated = True
        if negated:
            negative.append(grain)
        else:
            positive.append(grain)
            positive_spans.append((start, end))
    requested = tuple(dict.fromkeys(positive))
    excluded = tuple(dict.fromkeys(negative))
    # An explicit alternative is not a request for both grains.
    alternative = any(
        _ALTERNATIVE.search(question[left[1] : right[0]])
        for left, right in pairwise(positive_spans)
    ) or bool(
        positive_spans
        and re.match(
            r"\s*(?:の)?(?:どちら|どれか|いずれか)",
            question[positive_spans[-1][1] :],
        )
    )
    ambiguous = (
        uncertain
        or bool(set(requested) & set(excluded))
        or (len(requested) > 1 and alternative)
    )
    return GrainRequest(requested, excluded, bool(mentions), ambiguous)
