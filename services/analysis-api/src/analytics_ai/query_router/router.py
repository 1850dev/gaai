"""Policy-first Cache/GA4/BigQuery route selection."""

from __future__ import annotations

from dataclasses import dataclass

from analytics_ai.compat import StrEnum


class Route(StrEnum):
    CACHE = "cache"
    GA4 = "ga4"
    BIGQUERY = "bigquery"
    BIGQUERY_AI = "bigquery_ai"


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    intent: str
    cache_hit: bool = False
    visualization_only: bool = False
    requires_raw_events: bool = False
    requires_complex_join: bool = False
    requires_text_ai: bool = False
    realtime: bool = False
    funnel: bool = False
    bigquery_ai_enabled: bool = False


class QueryRouter:
    def choose(self, request: RoutingRequest) -> Route:
        if request.cache_hit or request.visualization_only:
            return Route.CACHE
        if request.requires_text_ai:
            if not request.bigquery_ai_enabled:
                raise ValueError("bigquery_ai_not_enabled")
            return Route.BIGQUERY_AI
        if request.requires_raw_events or request.requires_complex_join:
            return Route.BIGQUERY
        return Route.GA4
