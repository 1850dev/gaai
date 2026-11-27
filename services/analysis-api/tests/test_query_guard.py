from __future__ import annotations

import pytest

from analytics_ai.bigquery.query_guard import BigQueryGuard, QueryRejected
from analytics_ai.domain.models import BigQuerySource


@pytest.fixture
def source() -> BigQuerySource:
    return BigQuerySource(
        project_id="company-a-data",
        dataset_ids=("analytics_222222222",),
        allowed_resources=("company-a-data.analytics_222222222.events_*",),
    )


def valid_sql() -> str:
    return """
        SELECT event_name, COUNT(*) AS event_count
        FROM `company-a-data.analytics_222222222.events_*`
        WHERE _TABLE_SUFFIX BETWEEN '20260801' AND '20260824'
        GROUP BY event_name
        ORDER BY event_count DESC
        LIMIT 100
    """


def test_accepts_bounded_allowlisted_query(source: BigQuerySource) -> None:
    result = BigQueryGuard().validate(valid_sql(), source)
    assert result.result_limit == 100
    assert result.resources == ("company-a-data.analytics_222222222.events_*",)


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        (
            "DELETE FROM `project.dataset.table` WHERE TRUE",
            "not_read_only_query",
        ),
        (
            "SELECT * FROM `company-a-data.analytics_222222222.events_*` "
            "WHERE _TABLE_SUFFIX = '20260801' LIMIT 10",
            "select_star_forbidden",
        ),
        (
            "SELECT event_name FROM `company-a-data.analytics_222222222.events_*` "
            "LIMIT 10",
            "ga4_events_requires_date_suffix_filter",
        ),
        (
            "SELECT event_name FROM `company-a-data.analytics_111111111.events_*` "
            "WHERE _TABLE_SUFFIX = '20260801' LIMIT 10",
            "resource_outside_allowlist",
        ),
        (
            "SELECT event_name FROM `company-a-data.analytics_222222222.events_*` "
            "WHERE _TABLE_SUFFIX = '20260801'",
            "result_limit_required",
        ),
        (
            "SELECT a.event_name FROM "
            "`company-a-data.analytics_222222222.events_*` a CROSS JOIN "
            "`company-a-data.analytics_222222222.events_*` b "
            "WHERE _TABLE_SUFFIX = '20260801' LIMIT 10",
            "cross_join_forbidden",
        ),
    ],
)
def test_rejects_unsafe_queries(
    source: BigQuerySource,
    sql: str,
    code: str,
) -> None:
    with pytest.raises(QueryRejected) as rejected:
        BigQueryGuard().validate(sql, source)
    assert rejected.value.code == code
