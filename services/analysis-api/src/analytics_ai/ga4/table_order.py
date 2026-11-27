"""Reviewed GA4 ordering fields for bounded chronological channel tables."""

import re

TIME_TABLE_FIELDS = frozenset(
    {"date", "yearWeek", "isoYearIsoWeek", "yearMonth", "year"}
)
CHANNEL_TABLE_FIELDS = frozenset(
    {
        "sessionDefaultChannelGroup",
        "firstUserDefaultChannelGroup",
        "defaultChannelGroup",
        "sessionPrimaryChannelGroup",
        "firstUserPrimaryChannelGroup",
        "primaryChannelGroup",
    }
)
MAX_TIME_CHANNEL_ROWS = 1000


def is_channel_table_field(field: str) -> bool:
    return field in CHANNEL_TABLE_FIELDS or bool(
        re.fullmatch(r"(?:session|firstUser)CustomChannelGroup:\d+", field)
    )
