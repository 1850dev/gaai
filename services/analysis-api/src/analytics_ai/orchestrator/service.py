"""Authorized Planner → Cache → MCP → Evidence orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from analytics_ai.analysis_state.reducer import AnalysisDelta, AnalysisStateReducer
from analytics_ai.authorization.service import (
    AccessDenied,
    AuthorizationService,
    AuthorizedScope,
)
from analytics_ai.cache.key import CacheDescriptor, cache_key
from analytics_ai.cache.store import CacheBlobError, DatasetCache
from analytics_ai.domain.models import (
    AnalysisFilter,
    AnalysisFinding,
    AnalysisMessage,
    AnalysisResultReference,
    AnalysisSession,
    AnalysisState,
    AuditEvent,
    CalculationFact,
    CalculationKind,
    ComparisonKind,
    DatasetEvidence,
    DatePeriod,
    FrozenModel,
    Ga4AuthMode,
    MessageRole,
    SemanticDefinition,
    utc_now,
)
from analytics_ai.evidence.calculations import (
    change_rate,
    contribution,
    difference,
    rank_descending,
    ratio,
)
from analytics_ai.mcp_gateway.executor import McpExecutionDenied, ToolExecutionResult
from analytics_ai.mcp_gateway.gateway import McpServer, PreparedInvocation
from analytics_ai.orchestrator.answering import AnswerComposer, AnswerRenderer
from analytics_ai.orchestrator.browser_plan import BrowserPlan
from analytics_ai.orchestrator.category_comparison import (
    category_values,
    compare_categories,
)
from analytics_ai.orchestrator.normalization import JsonScalar, normalize_rows
from analytics_ai.orchestrator.planning import (
    CompiledMetric,
    CompiledPlan,
    IntentPlan,
    PlanCompiler,
    SummaryStatistic,
    apply_reviewed_site_evaluation_plan,
    is_ambiguous_site_evaluation,
)
from analytics_ai.orchestrator.skills import AnalysisSkillId
from analytics_ai.repositories.protocols import ApplicationRepository
from analytics_ai.semantic.observed import observed_definition
from analytics_ai.semantic.resolver import SemanticResolutionError
from analytics_ai.visualization.planner import (
    METRIC_LABELS,
    SeriesSpec,
    VisualizationPlanner,
    VisualizationSpec,
)

DEMOGRAPHIC_DIMENSIONS = frozenset({"userGender", "userAgeBracket"})


class IntentPlanner(Protocol):
    async def plan(
        self,
        *,
        question: str,
        state: AnalysisState,
        definitions: list[SemanticDefinition],
        today: date,
    ) -> IntentPlan: ...


class InvocationGateway(Protocol):
    async def prepare(
        self,
        *,
        user_id: str,
        session_id: str,
        server: McpServer,
        tool_name: str,
        proposed_arguments: dict[str, Any],
        trusted_dry_run: bool = True,
    ) -> PreparedInvocation: ...


class InvocationExecutor(Protocol):
    async def execute(self, invocation: PreparedInvocation) -> ToolExecutionResult: ...


class AnalysisOrchestrationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AnalysisResponse(FrozenModel):
    """One non-streaming response; SSE emits the same typed components later."""

    request_id: str
    session: AnalysisSession
    user_message: AnalysisMessage
    assistant_message: AnalysisMessage
    findings: tuple[AnalysisFinding, ...] = ()
    evidence: tuple[DatasetEvidence, ...] = ()
    visualization: VisualizationSpec | None = None
    rows: tuple[dict[str, JsonScalar], ...] = ()
    cache_hit: bool = False


class AnalysisHistoryResult(FrozenModel):
    """A persisted visualization rebuilt from scoped cache and evidence records."""

    message_id: str
    parent_message_id: str | None = None
    source_session_id: str | None = None
    source_message_id: str | None = None
    findings: tuple[AnalysisFinding, ...]
    evidence: tuple[DatasetEvidence, ...]
    visualization: VisualizationSpec
    rows: tuple[dict[str, JsonScalar], ...]
    cache_hit: bool


MAX_HISTORY_VISUALIZATIONS = 20
MAX_HISTORY_ROWS = 10_000


class AnalysisOrchestrator:
    def __init__(
        self,
        repository: ApplicationRepository,
        *,
        planner: IntentPlanner | None,
        gateway: InvocationGateway,
        executor: InvocationExecutor,
        cache: DatasetCache,
        now: Callable[[], datetime] = utc_now,
        timeout_seconds: float = 200.0,
        answer_composer: AnswerComposer | None = None,
        answer_composition_timeout_seconds: float = 30.0,
    ) -> None:
        self._repository = repository
        self._authorization = AuthorizationService(repository)
        self._planner = planner
        self._compiler = PlanCompiler()
        self._gateway = gateway
        self._executor = executor
        self._cache = cache
        self._reducer = AnalysisStateReducer()
        self._visualization = VisualizationPlanner()
        self._now = now
        self._timeout_seconds = timeout_seconds
        self._answer_composer = answer_composer
        self._answer_composition_timeout_seconds = answer_composition_timeout_seconds

    async def restore_latest_result(
        self,
        *,
        session: AnalysisSession,
        messages: Sequence[AnalysisMessage],
    ) -> AnalysisHistoryResult | None:
        results = await self.restore_results(session=session, messages=messages)
        return results[-1] if results else None

    async def restore_results(
        self,
        *,
        session: AnalysisSession,
        messages: Sequence[AnalysisMessage],
    ) -> tuple[AnalysisHistoryResult, ...]:
        """Restore recent message-bound charts under a bounded response budget."""

        await self._authorization.authorize_session(session.user_id, session)
        restored: list[AnalysisHistoryResult] = []
        total_rows = 0
        candidates = [
            item
            for item in messages
            if item.role is MessageRole.ASSISTANT
            and self._message_matches_session(item, session)
            and (
                (item.visualization is not None and item.finding_ids)
                or item.result_references
            )
        ][-MAX_HISTORY_VISUALIZATIONS:]
        source_messages: dict[str, list[AnalysisMessage]] = {session.id: list(messages)}
        for message in reversed(candidates):
            references = tuple(dict.fromkeys(message.result_references))
            for reference in (*reversed(references), None):
                if len(restored) >= MAX_HISTORY_VISUALIZATIONS:
                    break
                try:
                    if reference is not None:
                        result = await self._restore_attached_result(
                            session=session,
                            parent=message,
                            reference=reference,
                            source_messages=source_messages,
                        )
                    elif message.visualization is not None and message.finding_ids:
                        result = await self._restore_message_result(
                            session=session, message=message
                        )
                    else:
                        result = None
                except (AccessDenied, ValidationError, CacheBlobError):
                    # A revoked source or invalid old result never breaks unrelated
                    # authorized charts, and must not be returned as an attachment.
                    result = None
                if result is None or total_rows + len(result.rows) > MAX_HISTORY_ROWS:
                    continue
                restored.append(result)
                total_rows += len(result.rows)
        await self._authorization.authorize_session(session.user_id, session)
        return tuple(reversed(restored))

    async def _restore_attached_result(
        self,
        *,
        session: AnalysisSession,
        parent: AnalysisMessage,
        reference: AnalysisResultReference,
        source_messages: dict[str, list[AnalysisMessage]],
    ) -> AnalysisHistoryResult | None:
        source = await self._repository.get_session(reference.session_id)
        if source is None or (
            source.user_id != session.user_id
            or source.company_id != session.company_id
            or source.data_source_set_id != session.data_source_set_id
            or source.state.semantic_version != session.state.semantic_version
            or reference.semantic_version != session.state.semantic_version
        ):
            return None
        source_scope = await self._authorization.authorize_session(
            session.user_id, source
        )
        fingerprint = hashlib.sha256(
            str(source_scope.source_set.ga4).encode()
        ).hexdigest()
        if (
            not source_scope.source_set.ga4
            or reference.source_fingerprint != fingerprint
        ):
            return None
        if source.id not in source_messages:
            source_messages[source.id] = await self._repository.list_messages(source.id)
        message = next(
            (
                item
                for item in source_messages[source.id]
                if item.id == reference.message_id
            ),
            None,
        )
        if (
            message is None
            or message.role is not MessageRole.ASSISTANT
            or message.visualization is None
            or not message.finding_ids
            or message.result_references
        ):
            return None
        result = await self._restore_message_result(session=source, message=message)
        if result is None:
            return None
        if not result.evidence or any(
            e.ga4_property_id != source_scope.source_set.ga4.property_id
            or e.semantic_version != reference.semantic_version
            for e in result.evidence
        ):
            return None
        latest = await self._authorization.authorize_session(session.user_id, source)
        if (
            hashlib.sha256(str(latest.source_set.ga4).encode()).hexdigest()
            != fingerprint
        ):
            return None
        identity = "\0".join((parent.id, source.id, message.id))
        return result.model_copy(
            update={
                "message_id": "attached_"
                + hashlib.sha256(identity.encode()).hexdigest()[:24],
                "parent_message_id": parent.id,
                "source_session_id": source.id,
                "source_message_id": message.id,
                "cache_hit": True,
            }
        )

    async def _restore_message_result(
        self,
        *,
        session: AnalysisSession,
        message: AnalysisMessage,
    ) -> AnalysisHistoryResult | None:
        if not self._message_matches_session(message, session):
            return None
        findings: list[AnalysisFinding] = []
        for finding_id in message.finding_ids:
            finding = await self._repository.get_finding(finding_id)
            if finding is None or not self._finding_matches_session(finding, session):
                return None
            findings.append(finding)
        visualization = VisualizationSpec.model_validate(message.visualization)
        if not set(visualization.dataset_ids).issubset(message.dataset_ids) or not set(
            visualization.evidence_ids
        ).issubset(message.evidence_ids):
            return None
        finding = next(
            (item for item in findings if item.dataset_id in visualization.dataset_ids),
            None,
        )
        if finding is None:
            return None
        cached = await self._cache.get_by_key(
            finding.query_hash,
            company_id=session.company_id,
            data_source_set_id=session.data_source_set_id,
            allow_expired=True,
        )
        if cached is None or cached.metadata.dataset_id != finding.dataset_id:
            return None
        evidence: list[DatasetEvidence] = []
        for evidence_id in visualization.evidence_ids:
            item = await self._repository.get_evidence(evidence_id)
            if item is None or not self._evidence_matches(
                item,
                session=session,
                dataset_id=finding.dataset_id,
                query_hash=finding.query_hash,
            ):
                return None
            evidence.append(item)
        visualization = self._describe_saved_visualization(
            visualization,
            tuple(findings),
            cached.metadata.descriptor_json,
            finding.query_hash,
        )
        return AnalysisHistoryResult(
            message_id=message.id,
            findings=tuple(findings),
            evidence=tuple(evidence),
            visualization=visualization,
            rows=cached.rows,
            cache_hit=evidence[0].cache_hit if evidence else False,
        )

    async def analyze(
        self,
        *,
        user_id: str,
        session_id: str,
        content: str,
        request_id: str,
        idempotency_key: str | None = None,
        trusted_plan: IntentPlan | None = None,
        browser_plan: BrowserPlan | None = None,
        browser_agent: bool = False,
        expected_revision: int | None = None,
        observed_filters: tuple[AnalysisFilter, ...] = (),
    ) -> AnalysisResponse:
        session = await self._repository.get_session(session_id)
        if session is None:
            raise AnalysisOrchestrationError("session_not_found")
        scope = await self._authorization.authorize_session(user_id, session)
        if browser_agent and (
            trusted_plan is None
            or browser_plan is not None
            or scope.source_set.ga4 is None
            or scope.source_set.ga4.auth_mode is not Ga4AuthMode.USER_OAUTH
        ):
            raise AccessDenied("invalid_browser_agent_scope")
        if expected_revision is not None and session.revision != expected_revision:
            raise AnalysisOrchestrationError("analysis_session_conflict")
        if browser_plan and (
            not scope.source_set.ga4
            or scope.source_set.ga4.auth_mode != Ga4AuthMode.USER_OAUTH
            or trusted_plan
            or observed_filters
        ):
            raise AccessDenied("invalid_browser_scope")
        if session.title == "新しい分析":
            session = session.model_copy(
                update={
                    "title": self._session_title(scope.source_set.name, content),
                }
            )
        user_message = self._message(
            request_id=request_id,
            session=session,
            role=MessageRole.ASSISTANT
            if trusted_plan is not None
            else MessageRole.USER,
            content=content,
            message_id=self._idempotent_message_id(
                session.id,
                idempotency_key or request_id,
            ),
        )
        if trusted_plan is not None:
            user_message = user_message.model_copy(update={"kind": "agent_execution"})
        created = await self._repository.create_message(user_message)
        if not created:
            await self._audit_failure(
                session,
                request_id,
                "analysis_request_duplicate",
            )
            raise AnalysisOrchestrationError("analysis_request_duplicate")

        definitions: list[SemanticDefinition] = []
        try:
            async with asyncio.timeout(self._timeout_seconds):
                definitions = await self._repository.list_semantic_definitions(
                    company_id=scope.source_set.company_id,
                    data_source_set_id=scope.source_set.id,
                    version=session.state.semantic_version,
                )
                # Only server-validated observations or previously persisted scoped
                # state may introduce an exact value. No public request exposes this.
                observed = {
                    item.semantic_name: item
                    for item in (*session.state.filters, *observed_filters)
                    if item.semantic_name.startswith("observed_")
                }
                definitions.extend(
                    observed_definition(session, item) for item in observed.values()
                )
                if browser_plan is not None:
                    plan = browser_plan.to_intent()
                elif trusted_plan is not None:
                    plan = trusted_plan
                elif self._planner is not None:
                    plan = await self._planner.plan(
                        question=content,
                        state=session.state,
                        definitions=definitions,
                        today=self._today(scope),
                    )
                else:
                    raise AnalysisOrchestrationError("cloud_planner_disabled")
                if trusted_plan is None and browser_plan is None:
                    plan = apply_reviewed_site_evaluation_plan(content, plan)
                if (
                    trusted_plan is None
                    and browser_plan is None
                    and is_ambiguous_site_evaluation(
                        content,
                        definitions,
                        semantic_version=session.state.semantic_version,
                    )
                ):
                    return await self._respond_site_evaluation_clarification(
                        request_id=request_id,
                        session=session,
                        user_message=user_message,
                        period=plan.period,
                    )
                compile_state = session.state
                if browser_plan is not None and browser_plan.dimension is None:
                    # The preview promises an explicit grain or a period total;
                    # do not silently inherit a previous ranking dimension.
                    compile_state = compile_state.model_copy(
                        update={
                            "dimensions": (),
                            "focus": compile_state.focus.model_copy(
                                update={"dimension": None}
                            ),
                        }
                    )
                compiled = self._compiler.compile(
                    plan,
                    state=compile_state,
                    scope=scope,
                    definitions=definitions,
                )
                if (
                    browser_agent
                    and compiled.descriptor
                    and compiled.descriptor.source != "ga4"
                ):
                    raise AccessDenied("invalid_browser_agent_query")
                if browser_plan and (
                    not compiled.descriptor
                    or compiled.descriptor.source != "ga4"
                    or compiled.tool_name != "run_report"
                    or not compiled.skill
                    or compiled.skill.id.value != browser_plan.skill_id
                    or tuple(m.label for m in compiled.metrics)
                    != (browser_plan.metric,)
                    or (
                        browser_plan.dimension
                        and tuple(d.label for d in compiled.dimensions)
                        != (browser_plan.dimension,)
                    )
                ):
                    raise AccessDenied("invalid_browser_query")
                if compiled.descriptor is None:
                    return await self._visualize_existing(
                        request_id=request_id,
                        session=session,
                        user_message=user_message,
                        compiled=compiled,
                    )
                return await self._execute_plan(
                    request_id=request_id,
                    session=session,
                    scope=scope,
                    user_message=user_message,
                    compiled=compiled,
                    local_inference=browser_plan is not None or browser_agent,
                )
        except TimeoutError as error:
            await self._audit_failure(session, request_id, "analysis_timeout")
            raise AnalysisOrchestrationError("analysis_timeout") from error
        except asyncio.CancelledError:
            await asyncio.shield(
                self._audit_failure(session, request_id, "analysis_cancelled")
            )
            raise
        except SemanticResolutionError as error:
            if str(error) == "semantic_term_unavailable_for_data_source_set":
                return await self._respond_semantic_unavailable(
                    request_id=request_id,
                    session=session,
                    user_message=user_message,
                    definitions=definitions,
                )
            await self._audit_failure(session, request_id, str(error))
            raise AnalysisOrchestrationError(str(error)) from error
        except AnalysisOrchestrationError as error:
            await self._audit_failure(session, request_id, error.code)
            raise
        except McpExecutionDenied as error:
            await self._audit_failure(session, request_id, error.code)
            raise AnalysisOrchestrationError(error.code) from error
        except AccessDenied:
            await self._audit_failure(session, request_id, "access_denied")
            raise
        except Exception as error:
            await self._audit_failure(session, request_id, type(error).__name__)
            raise AnalysisOrchestrationError("analysis_failed") from error

    async def _execute_plan(
        self,
        *,
        request_id: str,
        session: AnalysisSession,
        scope: AuthorizedScope,
        user_message: AnalysisMessage,
        compiled: CompiledPlan,
        local_inference: bool = False,
    ) -> AnalysisResponse:
        descriptor = compiled.descriptor
        if descriptor is None or compiled.tool_name is None:
            raise AnalysisOrchestrationError("compiled_plan_is_incomplete")
        if local_inference:
            # A new, explicitly confirmed browser turn must use this user's CURRENT
            # Google read rights, never an earlier user's cached OAuth result.
            # Keep the snapshot immutable for history/re-interpretation of this turn.
            descriptor = descriptor.model_copy(
                update={
                    "filters": {**descriptor.filters, "browser_turn": user_message.id}
                }
            )
        query_hash = cache_key(descriptor)
        cached = (
            await self._cache.get(descriptor)
            if compiled.cacheable and not local_inference
            else None
        )
        cache_hit = cached is not None
        if cached is None:
            started = perf_counter()
            server = McpServer.GA4 if descriptor.source == "ga4" else McpServer.BIGQUERY
            invocation = await self._gateway.prepare(
                user_id=session.user_id,
                session_id=session.id,
                server=server,
                tool_name=compiled.tool_name,
                proposed_arguments=compiled.arguments,
            )
            result = await self._executor.execute(invocation)
            rows = normalize_rows(
                result.payload,
                expected_dimensions=self._argument_names(
                    compiled.arguments.get("dimensions")
                ),
                expected_metrics=self._argument_names(
                    compiled.arguments.get("metrics")
                ),
            )
            evidence_id = f"evidence_{uuid.uuid4().hex}"
            cached = await self._cache.put(
                descriptor,
                rows,
                evidence_id=evidence_id,
            )
            evidence = DatasetEvidence(
                id=evidence_id,
                company_id=session.company_id,
                data_source_set_id=session.data_source_set_id,
                dataset_id=cached.metadata.dataset_id,
                source=descriptor.source,
                semantic_version=session.state.semantic_version,
                mcp_server=invocation.server.value,
                tool_name=invocation.tool_name,
                ga4_property_id=(
                    scope.source_set.ga4.property_id
                    if invocation.server is McpServer.GA4
                    and scope.source_set.ga4 is not None
                    else None
                ),
                bigquery_project_id=(
                    scope.source_set.bigquery.project_id
                    if invocation.server is McpServer.BIGQUERY
                    and scope.source_set.bigquery is not None
                    else None
                ),
                query_hash=query_hash,
                query_id=result.query_id,
                bytes_processed=result.bytes_processed,
                rows=len(rows),
                latency_ms=int((perf_counter() - started) * 1_000),
                skill_id=compiled.skill.id.value if compiled.skill else None,
                skill_version=compiled.skill.version if compiled.skill else None,
                compiler_version=descriptor.compiler_version,
                quality=result.quality,
                property_quota=result.property_quota or {},
            )
            await self._repository.create_evidence(evidence)
        else:
            rows = list(cached.rows)
            cached_evidence_id = cached.metadata.evidence_id
            if cached_evidence_id is None:
                raise AnalysisOrchestrationError("cache_evidence_missing")
            loaded_evidence = await self._repository.get_evidence(cached_evidence_id)
            if loaded_evidence is None or not self._evidence_matches(
                loaded_evidence,
                session=session,
                dataset_id=cached.metadata.dataset_id,
                query_hash=query_hash,
            ):
                raise AnalysisOrchestrationError("cache_evidence_scope_mismatch")
            evidence = loaded_evidence

        await self._audit_cache(
            session,
            request_id=request_id,
            query_hash=query_hash,
            cache_hit=cache_hit,
            evidence_id=evidence.id,
        )
        state = self._reducer.apply(session.state, compiled.delta).state
        findings = tuple(
            self._build_finding(
                session=session,
                state=state,
                compiled=compiled,
                metric=metric,
                rows=rows,
                dataset_id=cached.metadata.dataset_id,
                evidence_id=evidence.id,
                query_hash=query_hash,
            )
            for metric in compiled.metrics
        )
        if not findings:
            raise AnalysisOrchestrationError("compiled_metric_is_missing")
        visualization = self._build_visualization(
            findings,
            len(rows),
            state=state,
            detail=compiled.detail,
            dimensions=tuple(item.field for item in compiled.dimensions),
            preferred_chart_type=compiled.preferred_chart_type,
        )
        visualization = self._describe_saved_visualization(
            visualization, findings, cached.metadata.descriptor_json, query_hash
        )
        state = self._reducer.apply(
            state,
            AnalysisDelta(
                dataset_ids=(cached.metadata.dataset_id,),
                findings=tuple(finding.to_ref() for finding in findings),
            ),
        ).state
        updated_session = session.model_copy(
            update={
                "state": state,
                "revision": session.revision + 1,
                "updated_at": self._now(),
            }
        )
        # The agent performs one cross-analysis assessment at the end. Avoid
        # consuming a model call/timeout for a second narrative after each query.
        answer = (
            "\n".join(finding.text for finding in findings)
            if user_message.kind == "agent_execution" or local_inference
            else await self._compose_answer(
                session=updated_session,
                request_id=request_id,
                question=user_message.content,
                findings=findings,
            )
        )
        assistant_message = self._message(
            request_id=request_id,
            session=session,
            role=MessageRole.ASSISTANT,
            content=answer,
            dataset_ids=(cached.metadata.dataset_id,),
            evidence_ids=(evidence.id,),
            finding_ids=tuple(finding.id for finding in findings),
            visualization=visualization.model_dump(mode="json"),
        )
        await self._commit(
            original=session,
            updated=updated_session,
            assistant_message=assistant_message,
            findings=findings,
        )
        await self._audit_success(
            updated_session,
            request_id=request_id,
            cache_hit=cache_hit,
            evidence_ids=(evidence.id,),
            local_inference=local_inference,
        )
        return AnalysisResponse(
            request_id=request_id,
            session=updated_session,
            user_message=user_message,
            assistant_message=assistant_message,
            findings=findings,
            evidence=(evidence,),
            visualization=visualization,
            rows=tuple(rows),
            cache_hit=cache_hit,
        )

    async def _compose_answer(
        self,
        *,
        session: AnalysisSession,
        request_id: str,
        question: str,
        findings: tuple[AnalysisFinding, ...],
    ) -> str:
        composer = self._answer_composer
        if composer is None:
            return "\n".join(finding.text for finding in findings)
        try:
            async with asyncio.timeout(self._answer_composition_timeout_seconds):
                draft = await composer.compose(
                    question=question,
                    state=session.state,
                    findings=findings,
                )
        except TimeoutError:
            return await self._answer_composition_fallback(
                session=session,
                request_id=request_id,
                findings=findings,
                model_name=composer.model_name,
                error_code="answer_composition_timeout",
            )
        except Exception as error:
            return await self._answer_composition_fallback(
                session=session,
                request_id=request_id,
                findings=findings,
                model_name=composer.model_name,
                error_code=type(error).__name__,
            )

        try:
            answer = AnswerRenderer.render(draft, findings)
        except Exception as error:
            return await self._answer_composition_fallback(
                session=session,
                request_id=request_id,
                findings=findings,
                model_name=composer.model_name,
                error_code=type(error).__name__,
            )

        await self._audit_answer_composition(
            session,
            request_id=request_id,
            model_name=composer.model_name,
            status="success",
            finding_ids=draft.finding_ids,
            evidence_ids=tuple(
                dict.fromkeys(
                    evidence_id
                    for finding in findings
                    if finding.id in draft.finding_ids
                    for evidence_id in finding.evidence_ids
                )
            ),
        )
        return answer

    async def _answer_composition_fallback(
        self,
        *,
        session: AnalysisSession,
        request_id: str,
        findings: tuple[AnalysisFinding, ...],
        model_name: str,
        error_code: str,
    ) -> str:
        await self._audit_answer_composition(
            session,
            request_id=request_id,
            model_name=model_name,
            status="error",
            finding_ids=tuple(finding.id for finding in findings),
            evidence_ids=tuple(
                dict.fromkeys(
                    evidence_id
                    for finding in findings
                    for evidence_id in finding.evidence_ids
                )
            ),
            error_code=error_code,
        )
        return "\n".join(finding.text for finding in findings)

    async def _visualize_existing(
        self,
        *,
        request_id: str,
        session: AnalysisSession,
        user_message: AnalysisMessage,
        compiled: CompiledPlan,
    ) -> AnalysisResponse:
        messages = await self._repository.list_messages(session.id)
        source_message = next(
            (
                item
                for item in reversed(messages)
                if item.role is MessageRole.ASSISTANT
                and item.finding_ids
                and item.visualization is not None
            ),
            None,
        )
        if source_message is None:
            raise AnalysisOrchestrationError("visualization_requires_existing_finding")
        loaded_findings = [
            await self._repository.get_finding(finding_id)
            for finding_id in source_message.finding_ids
        ]
        if any(item is None for item in loaded_findings):
            raise AnalysisOrchestrationError("visualization_requires_existing_finding")
        findings = tuple(item for item in loaded_findings if item is not None)
        finding = findings[0]
        if any(
            item.dataset_id != finding.dataset_id
            or item.query_hash != finding.query_hash
            or not self._finding_matches_session(item, session)
            for item in findings
        ):
            raise AnalysisOrchestrationError("visualization_finding_scope_mismatch")
        cached = await self._cache.get_by_key(
            finding.query_hash,
            company_id=session.company_id,
            data_source_set_id=session.data_source_set_id,
        )
        if cached is None or cached.metadata.dataset_id != finding.dataset_id:
            raise AnalysisOrchestrationError("visualization_dataset_not_available")
        evidence = await self._repository.get_evidence(finding.evidence_ids[0])
        if evidence is None or not self._evidence_matches(
            evidence,
            session=session,
            dataset_id=finding.dataset_id,
            query_hash=finding.query_hash,
        ):
            raise AnalysisOrchestrationError("cache_evidence_scope_mismatch")
        visualization = self._build_visualization(
            findings,
            len(cached.rows),
            state=session.state,
            detail=compiled.detail,
            dimensions=findings[0].dimension_fields,
            preferred_chart_type=compiled.preferred_chart_type,
        )
        visualization = self._describe_saved_visualization(
            visualization, findings, cached.metadata.descriptor_json, finding.query_hash
        )
        state = self._reducer.apply(session.state, compiled.delta).state
        updated_session = session.model_copy(
            update={
                "state": state,
                "revision": session.revision + 1,
                "updated_at": self._now(),
            }
        )
        assistant_message = self._message(
            request_id=request_id,
            session=session,
            role=MessageRole.ASSISTANT,
            content="現在の分析結果をグラフにしました。",
            dataset_ids=(finding.dataset_id,),
            evidence_ids=finding.evidence_ids,
            finding_ids=tuple(item.id for item in findings),
            visualization=visualization.model_dump(mode="json"),
        )
        await self._commit(
            original=session,
            updated=updated_session,
            assistant_message=assistant_message,
            findings=(),
        )
        await self._audit_success(
            updated_session,
            request_id=request_id,
            cache_hit=True,
            evidence_ids=finding.evidence_ids,
        )
        return AnalysisResponse(
            request_id=request_id,
            session=updated_session,
            user_message=user_message,
            assistant_message=assistant_message,
            findings=findings,
            evidence=(evidence,),
            visualization=visualization,
            rows=cached.rows,
            cache_hit=True,
        )

    async def _respond_site_evaluation_clarification(
        self,
        *,
        request_id: str,
        session: AnalysisSession,
        user_message: AnalysisMessage,
        period: DatePeriod | None,
    ) -> AnalysisResponse:
        state = self._reducer.apply(
            session.state,
            AnalysisDelta(period=period) if period is not None else AnalysisDelta(),
        ).state
        updated_session = session.model_copy(
            update={
                "state": state,
                "revision": session.revision + 1,
                "updated_at": self._now(),
            }
        )
        content = (
            "評価する観点を選んでください。\n"
            "- 集客（アクティブユーザー、セッション）\n"
            "- エンゲージメント（エンゲージメント率）\n"
            "- コンバージョン（キーイベント）\n"
            "- 総合レポート（上記4指標）\n"
            "比較も指定できます：比較なし、前期間比較、前週比較、"
            "前月比較、前年比較。"
        )
        assistant_message = self._message(
            request_id=request_id,
            session=session,
            role=MessageRole.ASSISTANT,
            content=content,
        )
        await self._commit(
            original=session,
            updated=updated_session,
            assistant_message=assistant_message,
            findings=(),
        )
        await self._audit_success(
            updated_session,
            request_id=request_id,
            cache_hit=False,
            evidence_ids=(),
        )
        return AnalysisResponse(
            request_id=request_id,
            session=updated_session,
            user_message=user_message,
            assistant_message=assistant_message,
        )

    async def _respond_semantic_unavailable(
        self,
        *,
        request_id: str,
        session: AnalysisSession,
        user_message: AnalysisMessage,
        definitions: Sequence[SemanticDefinition],
    ) -> AnalysisResponse:
        alternatives = sorted(
            {
                definition.aliases[0]
                for definition in definitions
                if definition.enabled
                and definition.available
                and definition.kind == "metric"
                and definition.aliases
            }
        )
        suggestion = "、".join(alternatives[:3])
        content = "このData Source SetではCVが定義されていません。"
        if suggestion:
            content += f"代替KPIとして{suggestion}を分析できます。"
        updated_session = session.model_copy(
            update={
                "revision": session.revision + 1,
                "updated_at": self._now(),
            }
        )
        assistant_message = self._message(
            request_id=request_id,
            session=session,
            role=MessageRole.ASSISTANT,
            content=content,
        )
        await self._commit(
            original=session,
            updated=updated_session,
            assistant_message=assistant_message,
            findings=(),
        )
        await self._audit_success(
            updated_session,
            request_id=request_id,
            cache_hit=False,
            evidence_ids=(),
        )
        return AnalysisResponse(
            request_id=request_id,
            session=updated_session,
            user_message=user_message,
            assistant_message=assistant_message,
        )

    async def _commit(
        self,
        *,
        original: AnalysisSession,
        updated: AnalysisSession,
        assistant_message: AnalysisMessage,
        findings: tuple[AnalysisFinding, ...],
    ) -> None:
        current = await self._repository.get_session(original.id)
        if current is None or current.revision != original.revision:
            raise AnalysisOrchestrationError("analysis_session_conflict")
        await self._authorization.authorize_session(original.user_id, current)
        committed = await self._repository.commit_analysis(
            updated,
            assistant_message,
            findings,
            expected_revision=original.revision,
        )
        if not committed:
            raise AnalysisOrchestrationError("analysis_session_conflict")

    def _build_finding(
        self,
        *,
        session: AnalysisSession,
        state: AnalysisState,
        compiled: CompiledPlan,
        metric: CompiledMetric,
        rows: Sequence[Mapping[str, JsonScalar]],
        dataset_id: str,
        evidence_id: str,
        query_hash: str,
    ) -> AnalysisFinding:
        current_rows = [row for row in rows if row.get("dateRange") != "comparison"]
        comparison_rows = [row for row in rows if row.get("dateRange") == "comparison"]
        joint = len(compiled.dimensions) > 1
        temporal_joint = joint and any(d.kind == "date" for d in compiled.dimensions)
        # Date/category is not a single additive time series. Keep exact cells;
        # never sum users/rates or publish a partial category sum as a site total.
        value_kind = "dimension" if joint else compiled.dimension_kind
        value, category, summary_kind = self._select_value(
            current_rows,
            metric_field=metric.field,
            dimension_field=compiled.dimension_field,
            dimension_kind=value_kind,
            metric_aggregation=metric.aggregation,
            summary_statistic=compiled.summary_statistic,
        )
        comparison_value: Decimal | None = None
        if comparison_rows and not temporal_joint:
            comparable_rows = comparison_rows
            if compiled.dimension_field and value_kind != "date":
                # The leading category can change between periods. Compare the
                # selected current row with the exact same dimensional tuple,
                # never with the independently selected previous-period leader.
                current_candidates = [
                    row for row in current_rows if row.get(metric.field) is not None
                ]
                selected_row = (
                    max(
                        current_candidates,
                        key=lambda row: (self._decimal(row[metric.field]), str(row)),
                    )
                    if current_candidates
                    else None
                )
                comparable_rows = [
                    row
                    for row in comparison_rows
                    if selected_row is not None
                    and all(
                        row.get(dimension.field) == selected_row.get(dimension.field)
                        for dimension in compiled.dimensions
                    )
                ]
                # Missing or ambiguous comparison data is unknown, not zero.
                if len(comparable_rows) != 1:
                    comparable_rows = []
            comparison_value, _, _ = self._select_value(
                comparable_rows,
                metric_field=metric.field,
                dimension_field=compiled.dimension_field,
                dimension_kind=value_kind,
                metric_aggregation=metric.aggregation,
                summary_statistic=compiled.summary_statistic,
            )
        rate = (
            change_rate(value, comparison_value)
            if value is not None and comparison_value is not None
            else None
        )
        comparison_kind = self._comparison_kind(state)
        category_comparisons = (
            compare_categories(
                current_rows,
                comparison_rows,
                fields=tuple(d.field for d in compiled.dimensions),
                metric=metric.field,
            )
            # GA4 may emit zero-valued date cells outside the named range.
            # A joint absolute day/category key cannot align separate months.
            if comparison_kind is not None
            and value_kind != "date"
            and not temporal_joint
            else ()
        )
        calculations = (
            self._calculation_facts(
                current_rows=current_rows,
                comparison_rows=comparison_rows,
                metric_field=metric.field,
                dimension_field=compiled.dimension_field,
                dimension_kind=compiled.dimension_kind,
                metric_aggregation=metric.aggregation,
                value=value,
                comparison_value=comparison_value,
                comparison_kind=comparison_kind,
            )
            if not joint
            else ()
        )
        cells = category_values(
            current_rows,
            fields=tuple(d.field for d in compiled.dimensions),
            metric=metric.field,
        )
        if joint:
            candidates = [r for r in current_rows if r.get(metric.field) is not None]
            selected = (
                max(candidates, key=lambda r: (self._decimal(r[metric.field]), str(r)))
                if candidates
                else None
            )
            category = (
                " / ".join(
                    str(selected.get(d.field, "未確認")) for d in compiled.dimensions
                )
                if selected
                else None
            )
        text, assumptions = self._finding_text(
            metric_label=metric.label,
            value=value,
            category=category,
            dimension_kind=value_kind,
            dimension_field=compiled.dimension_field,
            rate=rate,
            comparison_kind=comparison_kind,
        )
        if metric.aggregation == "sum":
            assumptions = tuple(
                "全体合計ではなく取得範囲の上位カテゴリを表示"
                if note == "非加算Metricを合計せず上位カテゴリを表示"
                else note
                for note in assumptions
            )
        if any(f.kind is CalculationKind.CONTRIBUTION for f in calculations):
            assumptions += (
                "寄与度は両期間に取得できたカテゴリ内の増減に対する割合です。"
                "全サイトの増減寄与とは限りません。欠落カテゴリはゼロ補完しません。",
            )
        if category_comparisons:
            assumptions += (
                "カテゴリ比較は取得範囲内の同一集計軸の組合せを照合しています。"
                "件数・増減の大きい項目などを最大20件表示し、未取得はゼロと扱いません。"
                "参照元名だけからメディアやチャネルを推定しません。",
            )
        if joint:
            assumptions += (
                "複合集計は各集計軸の組合せごとの値です。先頭軸の単独合計ではありません。"
                "表示は取得範囲の最大12組合せであり、各ページ全体の順位や全件網羅を保証しません。",
            )
        if value is not None and summary_kind is not None:
            calculations = (
                *calculations,
                CalculationFact(
                    kind=summary_kind,
                    value=self._required_decimal(value),
                    unit=metric.unit,
                ),
            )
            comparison_suffix = text[text.index("（") :] if "（" in text else ""
            if summary_kind is CalculationKind.PERIOD_TOTAL:
                text = (
                    f"期間合計の{metric.label}: {self._format_decimal(value)}"
                    f"{comparison_suffix}"
                )
                assumptions += ("aggregation=sumのMetricだけを期間合計",)
            elif summary_kind is CalculationKind.PERIOD_AVERAGE:
                text = (
                    f"期間平均の{metric.label}: {self._format_decimal(value)}"
                    f"{comparison_suffix}"
                )
                assumptions += ("取得した時系列行の算術平均",)
        anomaly = self._anomaly_fact(
            current_rows,
            metric_field=metric.field,
            dimension_field=compiled.dimension_field,
        )
        if compiled.skill and compiled.skill.id is AnalysisSkillId.ANOMALY_SCAN:
            if anomaly is None:
                assumptions += ("異常検知に必要な7期間以上の値がありません",)
            else:
                calculations = (*calculations, anomaly)
                score = Decimal(anomaly.value)
                status = "異常候補" if abs(score) >= Decimal("3.5") else "通常範囲"
                text += f"（ロバストスコア {anomaly.value}: {status}）"
                assumptions += ("median/MADによるロバスト異常検知",)
        return AnalysisFinding(
            id=f"finding_{uuid.uuid4().hex}",
            session_id=session.id,
            user_id=session.user_id,
            company_id=session.company_id,
            data_source_set_id=session.data_source_set_id,
            dataset_id=dataset_id,
            query_hash=query_hash,
            evidence_ids=(evidence_id,),
            semantic_version=session.state.semantic_version,
            skill_id=compiled.skill.id.value if compiled.skill else None,
            skill_version=compiled.skill.version if compiled.skill else None,
            compiler_version=(
                compiled.descriptor.compiler_version if compiled.descriptor else None
            ),
            text=text,
            metric_field=metric.field,
            metric_label=metric.label,
            dimension_field=compiled.dimension_field,
            dimension_kind=compiled.dimension_kind,
            dimension_fields=tuple(d.field for d in compiled.dimensions),
            category_values=cells,
            value=self._format_decimal(value),
            comparison_value=self._format_decimal(comparison_value),
            change_rate=self._format_decimal(rate),
            comparison_kind=comparison_kind,
            calculations=calculations,
            category_comparisons=category_comparisons,
            assumptions=assumptions,
        )

    @classmethod
    def _anomaly_fact(
        cls,
        rows: Sequence[Mapping[str, JsonScalar]],
        *,
        metric_field: str,
        dimension_field: str | None,
    ) -> CalculationFact | None:
        if dimension_field is None:
            return None
        ordered = sorted(rows, key=lambda row: str(row.get(dimension_field, "")))
        values = [
            cls._decimal(row.get(metric_field))
            for row in ordered
            if row.get(metric_field) is not None
        ]
        if len(values) < 7:
            return None
        current = values[-1]
        baseline = values[:-1]
        middle = cls._median(baseline)
        mad = cls._median([abs(item - middle) for item in baseline])
        if mad == 0:
            score = Decimal(0) if current == middle else Decimal("999")
        else:
            score = (current - middle) * Decimal("0.6745") / mad
        return CalculationFact(
            kind=CalculationKind.ANOMALY_SCORE,
            value=cls._required_decimal(score.quantize(Decimal("0.001"))),
            numerator=cls._required_decimal(current - middle),
            denominator=cls._required_decimal(mad),
        )

    @staticmethod
    def _median(values: Sequence[Decimal]) -> Decimal:
        ordered = sorted(values)
        midpoint = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[midpoint]
        return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)

    @classmethod
    def _calculation_facts(
        cls,
        *,
        current_rows: Sequence[Mapping[str, JsonScalar]],
        comparison_rows: Sequence[Mapping[str, JsonScalar]],
        metric_field: str,
        dimension_field: str | None,
        dimension_kind: str | None = None,
        metric_aggregation: str | None,
        value: Decimal | None,
        comparison_value: Decimal | None,
        comparison_kind: ComparisonKind | None,
    ) -> tuple[CalculationFact, ...]:
        facts: list[CalculationFact] = []
        if value is not None:
            facts.append(
                CalculationFact(
                    kind=CalculationKind.VALUE,
                    value=cls._required_decimal(value),
                )
            )
        if value is not None and comparison_value is not None:
            delta = difference(value, comparison_value)
            facts.append(
                CalculationFact(
                    kind=CalculationKind.DIFFERENCE,
                    value=cls._required_decimal(delta),
                    numerator=cls._required_decimal(value),
                    denominator=cls._required_decimal(comparison_value),
                    comparison_kind=comparison_kind,
                )
            )
            value_ratio = ratio(value, comparison_value)
            if value_ratio is not None:
                facts.append(
                    CalculationFact(
                        kind=CalculationKind.RATIO,
                        value=cls._required_decimal(value_ratio),
                        numerator=cls._required_decimal(value),
                        denominator=cls._required_decimal(comparison_value),
                        comparison_kind=comparison_kind,
                        unit="ratio",
                    )
                )
            rate = change_rate(value, comparison_value)
            if rate is not None:
                facts.append(
                    CalculationFact(
                        kind=CalculationKind.CHANGE_RATE,
                        value=cls._required_decimal(rate),
                        numerator=cls._required_decimal(delta),
                        denominator=cls._required_decimal(comparison_value),
                        comparison_kind=comparison_kind,
                        unit="ratio",
                    )
                )

        current_by_category = cls._category_values(
            current_rows,
            metric_field=metric_field,
            dimension_field=dimension_field,
        )
        if current_by_category:
            for category, category_value, rank in rank_descending(current_by_category)[
                :10
            ]:
                facts.append(
                    CalculationFact(
                        kind=CalculationKind.RANK,
                        value=cls._required_decimal(category_value),
                        category=category,
                        rank=rank,
                    )
                )

        if (
            metric_aggregation == "sum"
            and comparison_kind is not None
            and dimension_kind != "date"
        ):
            comparison_by_category = cls._category_values(
                comparison_rows,
                metric_field=metric_field,
                dimension_field=dimension_field,
            )
            # Limited reports can omit different categories in each period.
            # Missing is unknown, never a measured zero. These contributions
            # describe the matched retrieved subset, not the whole property.
            categories = set(current_by_category) & set(comparison_by_category)
            total_change = difference(
                sum(
                    (current_by_category.get(item, Decimal(0)) for item in categories),
                    Decimal(0),
                ),
                sum(
                    (
                        comparison_by_category.get(item, Decimal(0))
                        for item in categories
                    ),
                    Decimal(0),
                ),
            )
            if total_change != 0:
                ranked_categories = sorted(
                    categories,
                    key=lambda category: (
                        -abs(
                            current_by_category.get(category, Decimal(0))
                            - comparison_by_category.get(category, Decimal(0))
                        ),
                        category,
                    ),
                )
                for category in ranked_categories[:10]:
                    part_change = difference(
                        current_by_category.get(category, Decimal(0)),
                        comparison_by_category.get(category, Decimal(0)),
                    )
                    share = contribution(part_change, total_change)
                    if share is None:
                        continue
                    facts.append(
                        CalculationFact(
                            kind=CalculationKind.CONTRIBUTION,
                            value=cls._required_decimal(share),
                            category=category,
                            numerator=cls._required_decimal(part_change),
                            denominator=cls._required_decimal(total_change),
                            comparison_kind=comparison_kind,
                            unit="ratio",
                        )
                    )
        return tuple(facts)

    @classmethod
    def _category_values(
        cls,
        rows: Sequence[Mapping[str, JsonScalar]],
        *,
        metric_field: str,
        dimension_field: str | None,
    ) -> dict[str, Decimal]:
        if dimension_field is None:
            return {}
        values: dict[str, Decimal] = {}
        for row in rows:
            category = row.get(dimension_field)
            metric = row.get(metric_field)
            if category is None or metric is None:
                continue
            key = str(category)
            if key in values:
                # A repeated category may represent another unmodelled dimension.
                # Do not aggregate it unless the query is explicitly recompiled.
                return {}
            values[key] = cls._decimal(metric)
        return values

    @staticmethod
    def _comparison_kind(state: AnalysisState) -> ComparisonKind | None:
        if state.period is None or state.comparison_period is None:
            return None
        if state.comparison_period == AnalysisStateReducer.previous_month_period(
            state.period
        ):
            return ComparisonKind.MOM
        if state.comparison_period == AnalysisStateReducer.previous_year_period(
            state.period
        ):
            return ComparisonKind.YOY
        return ComparisonKind.CUSTOM

    def _build_visualization(
        self,
        findings: tuple[AnalysisFinding, ...],
        row_count: int,
        *,
        state: AnalysisState,
        detail: bool = False,
        dimensions: tuple[str, ...] = (),
        preferred_chart_type: str | None = None,
    ) -> VisualizationSpec:
        if not findings:
            raise AnalysisOrchestrationError("visualization_requires_finding")
        finding = findings[0]
        metric_label = "・".join(
            METRIC_LABELS.get(f.metric_field, f.metric_label) for f in findings
        )
        return self._visualization.plan(
            title=self._visualization.analysis_title(
                period=state.period,
                metric_field=finding.metric_field,
                metric_label=metric_label,
                dimension_field=finding.dimension_field,
                dimension_kind=finding.dimension_kind,
                comparison_kind=finding.comparison_kind,
                filters=state.filters,
                dimensions=dimensions,
                metric_labels=tuple(
                    METRIC_LABELS.get(f.metric_field, f.metric_label) for f in findings
                ),
            ),
            dataset_id=finding.dataset_id,
            evidence_ids=finding.evidence_ids,
            metric_field=finding.metric_field,
            metric_label=finding.metric_label,
            dimension_field=finding.dimension_field,
            dimension_kind=finding.dimension_kind,
            category_count=row_count,
            detail=detail,
            dimensions=dimensions,
            preferred_chart_type=preferred_chart_type,
            series=tuple(
                SeriesSpec(
                    field=item.metric_field,
                    label=item.metric_label,
                )
                for item in findings
            ),
        )

    def _describe_saved_visualization(
        self,
        visualization: VisualizationSpec,
        findings: tuple[AnalysisFinding, ...],
        descriptor_json: str,
        query_hash: str,
    ) -> VisualizationSpec:
        """Describe the observed query, never the thread's latest drilldown state.

        This is a read-only projection for old charts too. Preserve chart type,
        axes, series, rows and evidence; no query or historical message rewrite.
        """
        try:
            descriptor = CacheDescriptor.model_validate_json(descriptor_json)
            if cache_key(descriptor) != query_hash or descriptor.source != "ga4":
                return visualization
            period = DatePeriod.model_validate(
                {"start": descriptor.date_range[0], "end": descriptor.date_range[1]}
            )
            comparison = (
                DatePeriod.model_validate(
                    {
                        "start": descriptor.comparison_date_range[0],
                        "end": descriptor.comparison_date_range[1],
                    }
                )
                if descriptor.comparison_date_range
                else None
            )
            filters = tuple(
                AnalysisFilter.model_validate(f)
                for f in descriptor.filters.get("semantic_filters", [])
            )
        except (ValueError, TypeError):
            # Realtime/legacy metadata must not acquire invented calendar scope.
            return visualization
        finding = findings[0]
        ranking = descriptor.filters.get("ranking", {})
        if not isinstance(ranking, dict):
            ranking = {}
        limit = ranking.get("limit")
        valid_limit = limit if type(limit) is int and 1 <= limit <= 100 else None
        return visualization.model_copy(
            update={
                "title": self._visualization.analysis_title(
                    period=period,
                    metric_field=finding.metric_field,
                    metric_label="・".join(
                        METRIC_LABELS.get(f.metric_field, f.metric_label)
                        for f in findings
                    ),
                    dimension_field=finding.dimension_field,
                    dimension_kind=finding.dimension_kind,
                    dimensions=visualization.dimensions,
                    filters=filters,
                    comparison_kind=finding.comparison_kind,
                    metric_labels=tuple(
                        METRIC_LABELS.get(f.metric_field, f.metric_label)
                        for f in findings
                    ),
                ),
                "subtitle": self._visualization.analysis_subtitle(
                    comparison_period=comparison,
                    ranking_limit=valid_limit,
                    ranking_direction=str(ranking.get("direction", "descending")),
                    ranking_metric=str(ranking.get("metric", "")),
                ),
            }
        )

    @classmethod
    def _select_value(
        cls,
        rows: Sequence[Mapping[str, JsonScalar]],
        *,
        metric_field: str,
        dimension_field: str | None,
        dimension_kind: str | None,
        metric_aggregation: str | None = None,
        summary_statistic: SummaryStatistic = SummaryStatistic.AUTO,
    ) -> tuple[Decimal | None, str | None, CalculationKind | None]:
        candidates = [
            (cls._decimal(row.get(metric_field)), row)
            for row in rows
            if row.get(metric_field) is not None
        ]
        if not candidates:
            return None, None, None
        if dimension_field is None:
            return candidates[0][0], None, None
        if dimension_kind == "date":
            if summary_statistic is SummaryStatistic.AVERAGE:
                average = sum((item[0] for item in candidates), Decimal(0)) / Decimal(
                    len(candidates)
                )
                return average, None, CalculationKind.PERIOD_AVERAGE
            if summary_statistic is SummaryStatistic.TOTAL or (
                summary_statistic is SummaryStatistic.AUTO
                and metric_aggregation == "sum"
            ):
                total = sum((item[0] for item in candidates), Decimal(0))
                return total, None, CalculationKind.PERIOD_TOTAL
            selected = max(
                candidates, key=lambda item: str(item[1].get(dimension_field))
            )
            category_value = selected[1].get(dimension_field)
            return (
                selected[0],
                str(category_value) if category_value is not None else None,
                CalculationKind.LATEST_VALUE,
            )
        else:
            selected = max(candidates, key=lambda item: (item[0], str(item[1])))
        category_value = selected[1].get(dimension_field)
        return (
            selected[0],
            str(category_value) if category_value is not None else None,
            None,
        )

    @classmethod
    def _finding_text(
        cls,
        *,
        metric_label: str,
        value: Decimal | None,
        category: str | None,
        dimension_kind: str | None,
        dimension_field: str | None,
        rate: Decimal | None,
        comparison_kind: ComparisonKind | None,
    ) -> tuple[str, tuple[str, ...]]:
        if value is None:
            return f"対象期間の{metric_label}データはありません。", ()
        formatted = cls._format_decimal(value)
        if category is None:
            text = f"{metric_label}: {formatted}"
            assumptions: tuple[str, ...] = ()
        elif dimension_kind == "date":
            text = f"{category}時点の{metric_label}: {formatted}"
            assumptions = ("日付別結果の最新行を表示",)
        else:
            text = f"{metric_label}の上位は{category}: {formatted}"
            assumptions = ("非加算Metricを合計せず上位カテゴリを表示",)
        if dimension_field in DEMOGRAPHIC_DIMENSIONS:
            assumptions += ("ユーザー属性は判定可能なユーザーに限られ、unknownを含む",)
        if rate is not None:
            percentage = cls._format_decimal(rate * Decimal(100))
            comparison_label = "比較期間比"
            if comparison_kind is not None:
                comparison_label = {
                    ComparisonKind.MOM: "前月比",
                    ComparisonKind.YOY: "前年比",
                    ComparisonKind.CUSTOM: "比較期間比",
                }[comparison_kind]
            text += f"（{comparison_label} {percentage}%）"
        return text, assumptions

    @staticmethod
    def _decimal(value: JsonScalar) -> Decimal:
        if isinstance(value, bool) or value is None:
            raise AnalysisOrchestrationError("metric_value_is_not_numeric")
        try:
            result = Decimal(str(value))
        except InvalidOperation as error:
            raise AnalysisOrchestrationError("metric_value_is_not_numeric") from error
        if not result.is_finite():
            raise AnalysisOrchestrationError("metric_value_is_not_finite")
        return result

    @staticmethod
    def _format_decimal(value: Decimal | None) -> str | None:
        if value is None:
            return None
        rendered = format(value, "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered

    @classmethod
    def _required_decimal(cls, value: Decimal) -> str:
        rendered = cls._format_decimal(value)
        if rendered is None:  # pragma: no cover - Decimal input is never None
            raise AnalysisOrchestrationError("numeric_calculation_failed")
        return rendered

    def _message(
        self,
        *,
        request_id: str,
        session: AnalysisSession,
        role: MessageRole,
        content: str,
        dataset_ids: tuple[str, ...] = (),
        evidence_ids: tuple[str, ...] = (),
        finding_ids: tuple[str, ...] = (),
        visualization: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> AnalysisMessage:
        return AnalysisMessage(
            id=message_id or f"msg_{uuid.uuid4().hex}",
            request_id=request_id,
            session_id=session.id,
            user_id=session.user_id,
            company_id=session.company_id,
            data_source_set_id=session.data_source_set_id,
            role=role,
            content=content,
            dataset_ids=dataset_ids,
            evidence_ids=evidence_ids,
            finding_ids=finding_ids,
            visualization=visualization,
            created_at=self._now(),
        )

    @staticmethod
    def _idempotent_message_id(session_id: str, idempotency_key: str) -> str:
        digest = hashlib.sha256(
            f"{session_id}\0user\0{idempotency_key}".encode()
        ).hexdigest()
        return f"msg_{digest[:32]}"

    @staticmethod
    def _session_title(source_name: str, question: str) -> str:
        normalized_source = " ".join(source_name.split())
        normalized_question = " ".join(question.split())
        if len(normalized_question) > 48:
            normalized_question = f"{normalized_question[:47]}…"
        available = max(1, 120 - len(normalized_question) - 1)
        if len(normalized_source) > available:
            normalized_source = f"{normalized_source[: max(1, available - 1)]}…"
        return f"{normalized_source}｜{normalized_question}"

    @staticmethod
    def _argument_names(value: Any) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            return ()
        return tuple(value)

    @staticmethod
    def _today(scope: AuthorizedScope) -> date:
        timezone = (
            scope.source_set.ga4.timezone if scope.source_set.ga4 else "Asia/Tokyo"
        )
        return datetime.now(ZoneInfo(timezone)).date()

    @staticmethod
    def _message_matches_session(
        message: AnalysisMessage,
        session: AnalysisSession,
    ) -> bool:
        return (
            message.session_id == session.id
            and message.user_id == session.user_id
            and message.company_id == session.company_id
            and message.data_source_set_id == session.data_source_set_id
        )

    @staticmethod
    def _finding_matches_session(
        finding: AnalysisFinding,
        session: AnalysisSession,
    ) -> bool:
        return (
            finding.session_id == session.id
            and finding.user_id == session.user_id
            and finding.company_id == session.company_id
            and finding.data_source_set_id == session.data_source_set_id
            and finding.semantic_version == session.state.semantic_version
        )

    @staticmethod
    def _evidence_matches(
        evidence: DatasetEvidence,
        *,
        session: AnalysisSession,
        dataset_id: str,
        query_hash: str,
    ) -> bool:
        return (
            evidence.company_id == session.company_id
            and evidence.data_source_set_id == session.data_source_set_id
            and evidence.dataset_id == dataset_id
            and evidence.query_hash == query_hash
        )

    async def _audit_cache(
        self,
        session: AnalysisSession,
        *,
        request_id: str,
        query_hash: str,
        cache_hit: bool,
        evidence_id: str,
    ) -> None:
        await self._repository.write_audit_event(
            AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=request_id,
                action="dataset_cache.lookup",
                user_id=session.user_id,
                company_id=session.company_id,
                data_source_set_id=session.data_source_set_id,
                session_id=session.id,
                data_access_profile_id=session.data_access_profile_id,
                status="success",
                details={
                    "cache_hit": cache_hit,
                    "query_hash": query_hash,
                    "evidence_ids": [evidence_id],
                },
            )
        )

    async def _audit_success(
        self,
        session: AnalysisSession,
        *,
        request_id: str,
        cache_hit: bool,
        evidence_ids: tuple[str, ...],
        local_inference: bool = False,
    ) -> None:
        await self._repository.write_audit_event(
            AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=request_id,
                action="analysis.message.complete",
                user_id=session.user_id,
                company_id=session.company_id,
                data_source_set_id=session.data_source_set_id,
                session_id=session.id,
                data_access_profile_id=session.data_access_profile_id,
                status="success",
                details={
                    "cache_hit": cache_hit,
                    "evidence_ids": list(evidence_ids),
                    **(
                        {"plan_origin": "browser_confirmed", "server_llm_calls": 0}
                        if local_inference
                        else {}
                    ),
                },
            )
        )

    async def _audit_answer_composition(
        self,
        session: AnalysisSession,
        *,
        request_id: str,
        model_name: str,
        status: str,
        finding_ids: tuple[str, ...],
        evidence_ids: tuple[str, ...],
        error_code: str | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "llm_model": model_name,
            "finding_ids": list(finding_ids),
            "evidence_ids": list(evidence_ids),
        }
        if error_code is not None:
            details["error_code"] = error_code
        await self._repository.write_audit_event(
            AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=request_id,
                action="analysis.answer.compose",
                user_id=session.user_id,
                company_id=session.company_id,
                data_source_set_id=session.data_source_set_id,
                session_id=session.id,
                data_access_profile_id=session.data_access_profile_id,
                status=status,
                details=details,
            )
        )

    async def _audit_failure(
        self,
        session: AnalysisSession,
        request_id: str,
        error_code: str,
    ) -> None:
        await self._repository.write_audit_event(
            AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=request_id,
                action="analysis.message.complete",
                user_id=session.user_id,
                company_id=session.company_id,
                data_source_set_id=session.data_source_set_id,
                session_id=session.id,
                data_access_profile_id=session.data_access_profile_id,
                status="error",
                details={"error_code": error_code},
            )
        )
