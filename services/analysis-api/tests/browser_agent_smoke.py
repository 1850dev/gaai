"""Loopback-only synthetic fixture for qwen-smoke.mjs --shared-agent.

Not included in the production image; uses no credentials or external API.
"""

import asyncio

from analytics_ai.agent.models import SiteContext
from analytics_ai.api.app import create_app
from analytics_ai.config import Settings
from analytics_ai.mcp_gateway.executor import ToolExecutionResult
from analytics_ai.orchestrator.planning import IntentPlan
from tests.conftest import repository as fixture_repository
from tests.test_cross_breakdown import register
from tests.test_orchestrator import StaticVerifier, add_session, build_orchestrator


def create_fixture(*, seed_site: bool = False):
    repository = fixture_repository.__wrapped__()
    session = asyncio.run(add_session(repository))
    register(repository)
    orchestrator, executor = build_orchestrator(repository, [], {})

    async def execute(invocation):
        demo_values = {
            "pagePathPlusQueryString": "/synthetic",
            "sessionSource": "google",
            "sessionMedium": "organic",
            "sessionDefaultChannelGroup": "Organic Search",
        }
        dimensions = invocation.arguments["dimensions"]
        rows = [
            {
                **{
                    name: f"/synthetic-{index + 1}"
                    if dimensions == ["pagePathPlusQueryString"]
                    else demo_values.get(name, "sample")
                    for name in dimensions
                },
                **{name: str(42 - index) for name in invocation.arguments["metrics"]},
            }
            for index in range(5 if dimensions == ["pagePathPlusQueryString"] else 1)
        ]
        return ToolExecutionResult(
            payload={"rows": rows},
            query_id=None,
            bytes_processed=None,
            row_count=len(rows),
        )

    executor.execute = execute
    app = create_app(
        Settings(
            environment="test",
            enable_analytics_tools=True,
            enable_browser_qwen=True,
            enable_browser_agent=True,
            cors_origins=["http://127.0.0.1:4192"],
        ),
        repository=repository,
        token_verifier=StaticVerifier(),
        orchestrator=orchestrator,
    )
    if seed_site:

        async def seed() -> None:
            await app.state.services.agent.save_site(
                session.user_id,
                session.data_source_set_id,
                SiteContext(
                    site_basics="店舗の営業時間や催事を案内する情報サイト",
                    goals="来店前の案内を見つけやすくする",
                    technical_constraints="店舗担当者が既存案内を更新する",
                    content_policy="来店準備に役立つ既存案内を優先する",
                    allowed_surfaces=(),
                    allow_new_content=False,
                ),
            )
            await orchestrator.analyze(
                user_id=session.user_id,
                session_id=session.id,
                content="五月のページ別閲覧数",
                request_id="synthetic-browser-seed",
                trusted_plan=IntentPlan.model_validate(
                    {
                        "intent": "五月のページ別閲覧数",
                        "metric_terms": ["page_views"],
                        "dimension_terms": ["page_path"],
                        "period": {"start": "2026-05-01", "end": "2026-05-31"},
                    }
                ),
                browser_agent=True,
            )

        asyncio.run(seed())
    return app
