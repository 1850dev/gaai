"""AST-backed BigQuery read-only, resource, result, and AI policy guard."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from analytics_ai.domain.models import BigQuerySource


class QueryRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ValidatedQuery:
    sql: str
    resources: tuple[str, ...]
    result_limit: int


class BigQueryGuard:
    def __init__(self, max_result_rows: int = 3_000) -> None:
        if not 1 <= max_result_rows <= 3_000:
            raise ValueError("BigQuery MCP result policy must be <= 3,000")
        self._max_result_rows = max_result_rows

    def validate(self, sql: str, source: BigQuerySource) -> ValidatedQuery:
        try:
            statements = sqlglot.parse(sql, read="bigquery")
        except sqlglot.errors.ParseError as error:
            raise QueryRejected("invalid_sql") from error
        if len(statements) != 1:
            raise QueryRejected("multiple_statements")
        statement = statements[0]
        if not isinstance(statement, exp.Query):
            raise QueryRejected("not_read_only_query")
        forbidden = (
            exp.Insert,
            exp.Update,
            exp.Delete,
            exp.Create,
            exp.Drop,
            exp.Alter,
            exp.Command,
            exp.Merge,
        )
        if any(statement.find(node) is not None for node in forbidden):
            raise QueryRejected("not_read_only_query")

        for star in statement.find_all(exp.Star):
            if star.find_ancestor(exp.Count) is None:
                raise QueryRejected("select_star_forbidden")

        for join in statement.find_all(exp.Join):
            if str(join.args.get("kind") or "").upper() == "CROSS":
                raise QueryRejected("cross_join_forbidden")

        cte_names = {
            cte.alias_or_name.casefold() for cte in statement.find_all(exp.CTE)
        }
        resources: list[str] = []
        wildcard_events = False
        for table in statement.find_all(exp.Table):
            if table.name.casefold() in cte_names and not table.db:
                continue
            if not table.catalog or not table.db:
                raise QueryRejected("resource_must_be_fully_qualified")
            resource = f"{table.catalog}.{table.db}.{table.name}"
            if not any(
                fnmatch.fnmatchcase(resource, allowed)
                for allowed in source.allowed_resources
            ):
                raise QueryRejected("resource_outside_allowlist")
            resources.append(resource)
            if table.name.startswith("events_") and "*" in table.name:
                wildcard_events = True

        if not resources:
            raise QueryRejected("query_has_no_registered_resource")
        if wildcard_events and not re.search(
            r"(?is)_TABLE_SUFFIX\s+(?:BETWEEN|IN|=|>=|>)", sql
        ):
            raise QueryRejected("ga4_events_requires_date_suffix_filter")

        limit_expression = statement.args.get("limit")
        if limit_expression is None:
            raise QueryRejected("result_limit_required")
        expression = limit_expression.args.get("expression")
        try:
            result_limit = int(expression.this)
        except (AttributeError, TypeError, ValueError) as error:
            raise QueryRejected("result_limit_must_be_literal") from error
        if result_limit < 1 or result_limit > self._max_result_rows:
            raise QueryRejected("result_limit_exceeds_policy")

        self._validate_ai_policy(statement, source)
        normalized = statement.sql(dialect="bigquery", pretty=False)
        return ValidatedQuery(
            sql=normalized,
            resources=tuple(sorted(set(resources))),
            result_limit=result_limit,
        )

    @staticmethod
    def _validate_ai_policy(statement: Any, source: BigQuerySource) -> None:
        sql_upper = statement.sql(dialect="bigquery").upper()
        requested = {
            match.group(1)
            for match in re.finditer(r"\b((?:AI|ML)\.[A-Z_]+)\b", sql_upper)
        }
        if not requested:
            return
        if not source.bigquery_ai_enabled:
            raise QueryRejected("bigquery_ai_not_enabled")
        allowed = {name.upper() for name in source.allowed_ai_functions}
        if not requested.issubset(allowed):
            raise QueryRejected("bigquery_ai_function_not_allowed")
        if not source.allowed_ai_columns:
            raise QueryRejected("bigquery_ai_columns_not_configured")
