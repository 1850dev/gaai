"""Generate validated visualization specs, never drawing code."""

from __future__ import annotations

import re
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field, model_validator

from analytics_ai.compat import StrEnum
from analytics_ai.domain.models import AnalysisFilter, ComparisonKind, DatePeriod

METRIC_LABELS = {
    "active_users": "アクティブユーザー数",
    "activeUsers": "アクティブユーザー数",
    "average_session_duration": "平均セッション時間",
    "averageSessionDuration": "平均セッション時間",
    "bounce_rate": "直帰率",
    "bounceRate": "直帰率",
    "engagement_rate": "エンゲージメント率",
    "engagementRate": "エンゲージメント率",
    "event_count": "イベント数",
    "eventCount": "イベント数",
    "key_events": "キーイベント数",
    "keyEvents": "キーイベント数",
    "new_users": "新規ユーザー数",
    "newUsers": "新規ユーザー数",
    "page_views": "ページビュー数",
    "screenPageViews": "ページビュー数",
    "sessions": "セッション数",
    "total_users": "総ユーザー数",
    "totalUsers": "総ユーザー数",
}

DIMENSION_LABELS = {
    "device_category": "デバイス",
    "deviceCategory": "デバイス",
    "event_name": "イベント名",
    "eventName": "イベント名",
    "landing_page": "ランディングページ",
    "landingPagePlusQueryString": "ランディングページ",
    "page_path": "ページ",
    "pagePathPlusQueryString": "ページ",
    "session_channel_group": "チャネル",
    "sessionDefaultChannelGroup": "チャネル",
    "user_age_bracket": "年齢層",
    "user_gender": "性別",
    "userAgeBracket": "年齢層",
    "userGender": "性別",
    "sessionSource": "参照元",
    "sessionMedium": "メディア",
    "sessionSourceMedium": "参照元／メディア",
    "sessionCampaignName": "キャンペーン",
    "pagePath": "ページ",
    "pageTitle": "ページタイトル",
    "landingPage": "ランディングページ",
    "country": "国",
    "region": "地域",
    "city": "市区町村",
    "browser": "ブラウザ",
    "operatingSystem": "OS",
    "platform": "プラットフォーム",
    "language": "言語",
    "hostName": "ホスト名",
}

TIME_GRAIN_LABELS = {
    "date": "日別",
    "year_week": "週別",
    "yearWeek": "週別",
    "year_month": "月別",
    "yearMonth": "月別",
    "year": "年別",
}


class ChartType(StrEnum):
    KPI = "kpi"
    LINE = "line"
    BAR = "bar"
    DONUT = "donut"
    TABLE = "table"


class SeriesSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    label: str
    unit: str | None = None


class VisualizationSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: ChartType
    title: str
    subtitle: str | None = None
    dataset_ids: tuple[str, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    x: str | None = None
    category: str | None = None
    dimensions: tuple[str, ...] = ()
    series: tuple[SeriesSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_axes(self) -> VisualizationSpec:
        if self.type is ChartType.LINE and not self.x:
            raise ValueError("line charts require x")
        if self.type in {ChartType.BAR, ChartType.DONUT} and not self.category:
            raise ValueError("category chart requires category")
        return self


class VisualizationPlanner:
    @classmethod
    def analysis_title(
        cls,
        *,
        period: DatePeriod | None,
        metric_field: str,
        metric_label: str,
        dimension_field: str | None,
        dimension_kind: str | None,
        comparison_kind: ComparisonKind | None = None,
        filters: tuple[AnalysisFilter, ...] = (),
        dimensions: tuple[str, ...] = (),
        metric_labels: tuple[str, ...] = (),
    ) -> str:
        """Build a stable title from trusted analysis semantics and state."""

        period_label = cls._period_label(period)
        metric = METRIC_LABELS.get(
            metric_label,
            METRIC_LABELS.get(metric_field, cls._readable_label(metric_label)),
        )
        if metric_labels:
            metric = "・".join(
                METRIC_LABELS.get(label, cls._readable_label(label))
                for label in metric_labels
            )
        subject = metric
        if len(dimensions) > 1:
            axes = " × ".join(
                DIMENSION_LABELS.get(
                    field, TIME_GRAIN_LABELS.get(field, cls._readable_label(field))
                )
                for field in dimensions
            )
            subject = f"{axes}ごとの{metric}"
        elif dimension_field is not None:
            if dimension_kind == "date":
                grain = TIME_GRAIN_LABELS.get(
                    dimension_field,
                    cls._readable_label(dimension_field),
                )
                subject = f"{grain}{metric}"
            else:
                dimension = DIMENSION_LABELS.get(
                    dimension_field,
                    cls._readable_label(dimension_field),
                )
                subject = f"{dimension}ごとの{metric}"
        comparison = (
            {
                ComparisonKind.MOM: "（前月比）",
                ComparisonKind.YOY: "（前年比）",
                ComparisonKind.CUSTOM: "（比較期間比）",
            }.get(comparison_kind, "")
            if comparison_kind is not None
            else ""
        )
        # Use resolved, exact filter values; never guess a page name from its URL
        # or label a session source as organic/paid without medium evidence.
        targets = "／".join(
            f"{DIMENSION_LABELS.get(f.field_name, cls._readable_label(f.field_name))}"
            f"「{f.value}」"
            for f in sorted(filters, key=lambda f: (f.field_name, f.value))
        )
        scoped = (
            f"{targets}｜{subject}{comparison}" if targets else f"{subject}{comparison}"
        )
        return " ".join(item for item in (period_label, scoped) if item)

    @classmethod
    def analysis_subtitle(
        cls,
        *,
        comparison_period: DatePeriod | None = None,
        ranking_limit: int | None = None,
        ranking_direction: str = "descending",
        ranking_metric: str = "",
    ) -> str | None:
        parts: list[str] = []
        if comparison_period:
            parts.append(f"比較元：{cls._period_label(comparison_period)}")
        if ranking_limit is not None:
            direction = "下位" if ranking_direction == "ascending" else "上位"
            metric = METRIC_LABELS.get(
                ranking_metric, cls._readable_label(ranking_metric)
            )
            parts.append(
                f"{metric}{direction}{ranking_limit}件（取得範囲。全件の内訳ではありません）"
            )
        return "｜".join(parts) or None

    @staticmethod
    def _period_label(period: DatePeriod | None) -> str:
        if period is None:
            return ""
        start = period.start
        end = period.end
        if (
            start.year == end.year
            and start.month == end.month
            and start.day == 1
            and (end + timedelta(days=1)).day == 1
        ):
            return f"{start.year}年{start.month}月"
        if start == end:
            return f"{start.year}年{start.month}月{start.day}日"
        if start.year == end.year and start.month == end.month:
            return f"{start.year}年{start.month}月{start.day}日〜{end.day}日"
        if start.year == end.year:
            return (
                f"{start.year}年{start.month}月{start.day}日〜{end.month}月{end.day}日"
            )
        return (
            f"{start.year}年{start.month}月{start.day}日〜"
            f"{end.year}年{end.month}月{end.day}日"
        )

    @staticmethod
    def _readable_label(value: str) -> str:
        unscoped = value.rsplit(":", maxsplit=1)[-1]
        return re.sub(r"[_-]+", " ", unscoped).strip() or value

    def plan(
        self,
        *,
        title: str,
        dataset_id: str,
        evidence_ids: tuple[str, ...],
        metric_field: str,
        metric_label: str,
        dimension_field: str | None,
        dimension_kind: str | None,
        category_count: int | None = None,
        composition: bool = False,
        detail: bool = False,
        dimensions: tuple[str, ...] = (),
        series: tuple[SeriesSpec, ...] | None = None,
        preferred_chart_type: str | None = None,
        subtitle: str | None = None,
    ) -> VisualizationSpec:
        resolved_series = series or (
            SeriesSpec(field=metric_field, label=metric_label),
        )
        if len(dimensions) > 1 or detail or preferred_chart_type == "table":
            chart_type = ChartType.TABLE
            x = dimension_field if dimension_kind == "date" else None
            category = dimension_field if dimension_kind != "date" else None
        elif dimension_field is None:
            chart_type = ChartType.KPI
            x = None
            category = None
        elif preferred_chart_type == "bar" and dimension_field:
            chart_type = ChartType.BAR
            x = None
            category = dimension_field
        elif preferred_chart_type == "line" and dimension_field:
            chart_type = ChartType.LINE
            x = dimension_field
            category = None
        elif preferred_chart_type == "donut" and dimension_field:
            chart_type = ChartType.DONUT
            x = None
            category = dimension_field
        elif dimension_kind == "date":
            chart_type = ChartType.LINE
            x = dimension_field
            category = None
        elif composition and category_count is not None and category_count <= 6:
            chart_type = ChartType.DONUT
            x = None
            category = dimension_field
        else:
            chart_type = ChartType.BAR
            x = None
            category = dimension_field
        return VisualizationSpec(
            type=chart_type,
            title=title,
            subtitle=subtitle,
            dataset_ids=(dataset_id,),
            evidence_ids=evidence_ids,
            x=x,
            category=category,
            dimensions=dimensions or ((dimension_field,) if dimension_field else ()),
            series=resolved_series,
        )
