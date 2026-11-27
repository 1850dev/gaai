"""FastAPI application exposing only authorized, scope-safe endpoints."""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypeVar, get_args
from zoneinfo import ZoneInfo

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Security,
    WebSocket,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from analytics_ai.admin.models import (
    DisableRegistrationRequest,
    GrantRequest,
    LegacyRegistrationRequest,
    RegistrationError,
    RegistrationInput,
    RevisionRequest,
    SourceRegistration,
    UpdateRegistrationRequest,
)
from analytics_ai.admin.service import RegistrationService
from analytics_ai.admin.verification import (
    Ga4RegistrationVerifier,
    RegistrationVerifier,
)
from analytics_ai.agent.capabilities import effective_definitions
from analytics_ai.agent.director import VertexAgentDirector
from analytics_ai.agent.memory_models import (
    Agreement,
    AgreementChange,
    WorkingMemoryChange,
)
from analytics_ai.agent.models import (
    AgentWorkspace,
    PropertyPreferences,
    ReportSchedule,
    SiteContext,
)
from analytics_ai.agent.presentation import attributed_message
from analytics_ai.agent.schedules import ScheduleService
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.site_context import site_understanding
from analytics_ai.agent.store import FirestoreAgentStore, MemoryAgentStore
from analytics_ai.api.browser_socket import serve_browser_agent
from analytics_ai.auth.firebase import (
    AuthenticationError,
    DevelopmentTokenVerifier,
    FirebaseTokenVerifier,
    TokenVerifier,
    VerifiedIdentity,
)
from analytics_ai.authorization.service import AccessDenied, AuthorizationService
from analytics_ai.cache.filesystem import FilesystemCacheBlobs
from analytics_ai.cache.store import (
    DatasetCache,
    FirestoreCacheMetadata,
    GcsCacheBlobs,
    InMemoryCacheBlobs,
    InMemoryCacheMetadata,
)
from analytics_ai.config import Settings
from analytics_ai.credentials.token_cipher import KmsTokenCipher
from analytics_ai.domain.models import (
    AnalysisMessage,
    AnalysisSession,
    AnalysisState,
    AuditEvent,
    DataSourceSet,
    Ga4AuthMode,
    MemberRole,
    SessionStatus,
    utc_now,
)
from analytics_ai.domain.scope import source_scope_fingerprint
from analytics_ai.ga4.adapter import Ga4McpAdapter
from analytics_ai.ga4.credentials import Ga4CredentialProvider
from analytics_ai.ga4.discovery import (
    Ga4DiscoveryError,
    Ga4PropertyCandidate,
    Ga4PropertyDiscoveryService,
)
from analytics_ai.ga4.metadata import Ga4MetadataCatalog, Ga4MetadataCatalogService
from analytics_ai.ga4.oauth import (
    Ga4ConnectionSummary,
    Ga4ConnectResponse,
    Ga4OAuthError,
    Ga4OAuthService,
)
from analytics_ai.ga4.verification import (
    Ga4PropertyVerification,
    Ga4PropertyVerificationService,
    Ga4VerificationError,
)
from analytics_ai.mcp_gateway.executor import McpExecutionService
from analytics_ai.mcp_gateway.gateway import McpGateway
from analytics_ai.observability.logging import (
    configure_logging,
    request_id_context,
)
from analytics_ai.orchestrator.answering import VertexAnswerComposer
from analytics_ai.orchestrator.browser_plan import BrowserPlan, BrowserSkill
from analytics_ai.orchestrator.planning import VertexIntentPlanner
from analytics_ai.orchestrator.service import (
    AnalysisHistoryResult,
    AnalysisOrchestrationError,
    AnalysisOrchestrator,
    AnalysisResponse,
)
from analytics_ai.repositories.firestore import FirestoreRepository
from analytics_ai.repositories.memory import InMemoryRepository
from analytics_ai.repositories.protocols import ApplicationRepository

logger = logging.getLogger(__name__)
RegistrationResult = TypeVar("RegistrationResult")
bearer = HTTPBearer(auto_error=False)
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


@dataclass(slots=True)
class Services:
    settings: Settings
    repository: ApplicationRepository
    token_verifier: TokenVerifier
    authorization: AuthorizationService
    orchestrator: AnalysisOrchestrator | None
    ga4_discovery: Ga4PropertyDiscoveryService | None
    ga4_verification: Ga4PropertyVerificationService | None
    ga4_oauth: Ga4OAuthService | None
    ga4_metadata: Ga4MetadataCatalogService | None
    agent: AgentService
    registrations: RegistrationService | None


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceSetSummary(ApiModel):
    id: str
    company_id: str
    name: str
    has_ga4: bool
    has_bigquery: bool
    ga4_auth_mode: Ga4AuthMode | None
    ga4_property_id: str | None

    @classmethod
    def from_domain(cls, item: DataSourceSet) -> SourceSetSummary:
        return cls(
            id=item.id,
            company_id=item.company_id,
            name=item.name,
            has_ga4=item.ga4 is not None,
            has_bigquery=item.bigquery is not None,
            ga4_auth_mode=item.ga4.auth_mode if item.ga4 is not None else None,
            ga4_property_id=item.ga4.property_id if item.ga4 is not None else None,
        )


class CreateSessionRequest(ApiModel):
    data_source_set_id: str = Field(min_length=1, max_length=128)


class MessageRequest(ApiModel):
    content: str = Field(min_length=1, max_length=8_000)


class IdentitySummary(ApiModel):
    uid: str
    email: str | None
    analysis_provider: Literal["browser_qwen"] | None = None
    can_manage_data_sources: bool = False
    browser_agent_available: bool = False


class BrowserMessageRequest(MessageRequest):
    plan: BrowserPlan
    revision: int = Field(ge=0)


class SessionHistoryResponse(ApiModel):
    messages: tuple[AnalysisMessage, ...]
    results: tuple[AnalysisHistoryResult, ...] = ()
    latest_result: AnalysisHistoryResult | None = None


def build_services(
    settings: Settings,
    *,
    repository: ApplicationRepository | None = None,
    token_verifier: TokenVerifier | None = None,
    orchestrator: AnalysisOrchestrator | None = None,
    ga4_discovery: Ga4PropertyDiscoveryService | None = None,
    ga4_verification: Ga4PropertyVerificationService | None = None,
    ga4_oauth: Ga4OAuthService | None = None,
    registration_verifier: RegistrationVerifier | None = None,
) -> Services:
    if repository is None:
        if settings.resolved_repository_backend == "memory":
            repository = InMemoryRepository()
        else:
            repository = FirestoreRepository(settings.firebase_project_id)
    if token_verifier is None:
        token_verifier = (
            DevelopmentTokenVerifier()
            if settings.allow_insecure_dev_auth
            else FirebaseTokenVerifier(
                settings.firebase_project_id,
                check_revoked=settings.environment != "local",
            )
        )
    token_cipher = (
        KmsTokenCipher(settings.ga4_oauth_kms_key_name)
        if settings.enable_ga4_user_oauth
        else None
    )
    ga4_credentials = Ga4CredentialProvider(
        settings,
        repository,
        token_cipher,
    )
    ga4_metadata: Ga4MetadataCatalogService | None = None
    cloud_enabled = not settings.enable_browser_qwen or bool(settings.vertex_user_ids)
    if settings.enable_analytics_tools and orchestrator is None:
        if settings.resolved_cache_backend == "memory":
            cache = DatasetCache(InMemoryCacheMetadata(), InMemoryCacheBlobs())
        else:
            cache = DatasetCache(
                FirestoreCacheMetadata(settings.firebase_project_id),
                FilesystemCacheBlobs(settings.cache_directory)
                if settings.resolved_cache_backend == "filesystem"
                else GcsCacheBlobs(settings.platform_project_id, settings.cache_bucket),
            )
        gateway = McpGateway(settings, repository)
        executor = McpExecutionService(
            settings,
            repository,
            ga4_credentials=ga4_credentials,
        )
        orchestrator = AnalysisOrchestrator(
            repository,
            planner=VertexIntentPlanner(settings) if cloud_enabled else None,
            gateway=gateway,
            executor=executor,
            cache=cache,
            timeout_seconds=settings.analysis_timeout_seconds,
            answer_composer=VertexAnswerComposer(settings) if cloud_enabled else None,
            answer_composition_timeout_seconds=(
                settings.answer_composition_timeout_seconds
            ),
        )
        ga4_metadata = Ga4MetadataCatalogService(gateway, executor)
    if settings.enable_ga4_user_oauth:
        if token_cipher is None:
            raise RuntimeError("GA4 OAuth token cipher is not configured")
        if ga4_oauth is None:
            ga4_oauth = Ga4OAuthService(
                settings,
                repository,
                token_cipher,
            )
        if ga4_discovery is None:
            ga4_client = Ga4McpAdapter()
            ga4_discovery = Ga4PropertyDiscoveryService(
                repository,
                credentials=ga4_credentials,
                ga4_client=ga4_client,
            )
    if settings.enable_admin_connection_checks and ga4_verification is None:
        ga4_client = Ga4McpAdapter()
        ga4_verification = Ga4PropertyVerificationService(
            repository,
            credentials=ga4_credentials,
            ga4_client=ga4_client,
        )
    return Services(
        settings=settings,
        registrations=(
            RegistrationService(
                repository,
                registration_verifier or Ga4RegistrationVerifier(ga4_credentials),
            )
            if settings.enable_admin_connection_checks
            else None
        ),
        repository=repository,
        token_verifier=token_verifier,
        authorization=AuthorizationService(repository),
        orchestrator=orchestrator,
        ga4_discovery=ga4_discovery,
        ga4_verification=ga4_verification,
        ga4_oauth=ga4_oauth,
        ga4_metadata=ga4_metadata,
        agent=AgentService(
            repository,
            FirestoreAgentStore(settings.firebase_project_id)
            if isinstance(repository, FirestoreRepository)
            else MemoryAgentStore(),
            VertexAgentDirector(settings)
            if settings.enable_analytics_tools and cloud_enabled
            else None,
            orchestrator,
            vertex_allowed=settings.vertex_allowed,
        ),
    )


async def current_identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> VerifiedIdentity:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "authentication_required"},
        )
    services: Services = request.app.state.services
    try:
        return await services.token_verifier.verify(credentials.credentials)
    except AuthenticationError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_authentication"},
        ) from error


Identity = Annotated[VerifiedIdentity, Depends(current_identity)]


def sse_event(event: str, data: dict[str, object]) -> str:
    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event}\ndata: {payload}\n\n"


def validate_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not SAFE_REQUEST_ID.fullmatch(value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_idempotency_key"},
        )
    return value


def create_app(
    settings: Settings | None = None,
    *,
    repository: ApplicationRepository | None = None,
    token_verifier: TokenVerifier | None = None,
    orchestrator: AnalysisOrchestrator | None = None,
    ga4_discovery: Ga4PropertyDiscoveryService | None = None,
    ga4_verification: Ga4PropertyVerificationService | None = None,
    ga4_oauth: Ga4OAuthService | None = None,
    registration_verifier: RegistrationVerifier | None = None,
) -> FastAPI:
    configure_logging()
    selected_settings = settings or Settings()
    application = FastAPI(
        title="GA4 × BigQuery Analytics AI",
        version="0.1.0",
        docs_url="/docs" if selected_settings.environment != "prod" else None,
        redoc_url=None,
    )
    application.state.services = build_services(
        selected_settings,
        repository=repository,
        token_verifier=token_verifier,
        orchestrator=orchestrator,
        ga4_discovery=ga4_discovery,
        ga4_verification=ga4_verification,
        ga4_oauth=ga4_oauth,
        registration_verifier=registration_verifier,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=selected_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-Request-ID",
        ],
    )

    @application.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        proposed_request_id = request.headers.get("X-Request-ID", "")
        request_id = (
            proposed_request_id
            if SAFE_REQUEST_ID.fullmatch(proposed_request_id)
            else f"req_{uuid.uuid4().hex}"
        )
        token = request_id_context.set(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_context.reset(token)

    @application.exception_handler(AccessDenied)
    async def access_denied_handler(
        _request: Request, error: AccessDenied
    ) -> JSONResponse:
        logger.warning(
            "authorization denied",
            extra={"error_code": error.internal_reason},
        )
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": {"code": "access_denied"}},
        )

    @application.exception_handler(AnalysisOrchestrationError)
    async def analysis_error_handler(
        _request: Request,
        error: AnalysisOrchestrationError,
    ) -> JSONResponse:
        client_errors = {
            "semantic_term_not_found",
            "semantic_term_ambiguous",
            "semantic_term_unavailable_for_data_source_set",
            "visualization_requires_existing_finding",
            "visualization_dataset_not_available",
        }
        if error.code in {
            "analysis_session_conflict",
            "analysis_request_duplicate",
            "agent_revision_conflict",
            "agent_session_conflict",
            "agent_turn_in_progress",
        }:
            response_status = status.HTTP_409_CONFLICT
        elif error.code in {
            "analytics_tools_disabled",
            "scheduled_reports_require_durable_cache",
        }:
            response_status = status.HTTP_503_SERVICE_UNAVAILABLE
        elif error.code == "ga4_property_access_denied":
            response_status = status.HTTP_403_FORBIDDEN
        elif error.code in {"analysis_timeout", "mcp_execution_timeout"}:
            response_status = status.HTTP_504_GATEWAY_TIMEOUT
        elif error.code in client_errors:
            response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
        else:
            response_status = status.HTTP_502_BAD_GATEWAY
        logger.warning(
            "analysis orchestration failed",
            extra={"error_code": error.code},
        )
        return JSONResponse(
            status_code=response_status,
            content={"detail": {"code": error.code}},
        )

    @application.exception_handler(Ga4DiscoveryError)
    async def ga4_discovery_error_handler(
        _request: Request,
        error: Ga4DiscoveryError,
    ) -> JSONResponse:
        logger.warning(
            "GA4 property discovery failed",
            extra={"error_code": error.code},
        )
        return JSONResponse(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if error.code == "ga4_user_oauth_disabled"
                else status.HTTP_502_BAD_GATEWAY
            ),
            content={"detail": {"code": error.code}},
        )

    @application.exception_handler(Ga4VerificationError)
    async def ga4_verification_error_handler(
        _request: Request,
        error: Ga4VerificationError,
    ) -> JSONResponse:
        logger.warning(
            "GA4 property verification failed",
            extra={"error_code": error.code},
        )
        return JSONResponse(
            status_code=(
                status.HTTP_403_FORBIDDEN
                if error.code == "ga4_property_access_denied"
                else status.HTTP_502_BAD_GATEWAY
            ),
            content={"detail": {"code": error.code}},
        )

    @application.exception_handler(Ga4OAuthError)
    async def ga4_oauth_error_handler(
        _request: Request,
        error: Ga4OAuthError,
    ) -> JSONResponse:
        response_status = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if error.code == "ga4_user_oauth_disabled"
            else (
                status.HTTP_403_FORBIDDEN
                if error.code == "ga4_oauth_access_denied"
                else status.HTTP_400_BAD_REQUEST
            )
        )
        logger.warning(
            "GA4 OAuth operation failed",
            extra={"error_code": error.code},
        )
        return JSONResponse(
            status_code=response_status,
            content={"detail": {"code": error.code}},
        )

    @application.get("/healthz", include_in_schema=False)
    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get(
        "/api/me", response_model=IdentitySummary, response_model_exclude_unset=True
    )
    async def get_identity(identity: Identity, request: Request) -> IdentitySummary:
        services: Services = request.app.state.services
        result = IdentitySummary(uid=identity.uid, email=identity.email)
        if not services.settings.vertex_allowed(identity.uid):
            result = result.model_copy(update={"analysis_provider": "browser_qwen"})
            if (
                services.settings.enable_browser_qwen
                and services.settings.enable_browser_agent
                and services.settings.enable_analytics_tools
            ):
                result = result.model_copy(update={"browser_agent_available": True})
        if (
            services.settings.enable_admin_connection_checks
            and services.registrations is not None
            and await services.registrations.companies(identity.uid)
        ):
            result = result.model_copy(update={"can_manage_data_sources": True})
        return result

    @application.get(
        "/api/data-source-sets",
        response_model=list[SourceSetSummary],
    )
    async def list_source_sets(
        identity: Identity,
        request: Request,
    ) -> list[SourceSetSummary]:
        services: Services = request.app.state.services
        items = await services.authorization.list_allowed_source_sets(identity.uid)
        return [SourceSetSummary.from_domain(item) for item in items]

    @application.get(
        "/api/integrations/ga4/status",
        response_model=Ga4ConnectionSummary,
    )
    async def ga4_connection_status(
        identity: Identity,
        request: Request,
    ) -> Ga4ConnectionSummary | JSONResponse:
        services: Services = request.app.state.services
        if not services.settings.enable_ga4_user_oauth or services.ga4_oauth is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": {"code": "ga4_user_oauth_disabled"}},
            )
        return await services.ga4_oauth.status(identity.uid)

    @application.post(
        "/api/integrations/ga4/connect",
        response_model=Ga4ConnectResponse,
    )
    async def connect_ga4(
        identity: Identity,
        request: Request,
    ) -> Ga4ConnectResponse:
        services: Services = request.app.state.services
        if services.ga4_oauth is None:
            raise Ga4OAuthError("ga4_user_oauth_disabled")
        return await services.ga4_oauth.connect(identity)

    @application.get("/api/integrations/ga4/callback", response_model=None)
    async def ga4_oauth_callback(
        request: Request,
        state: Annotated[str, Query(min_length=32, max_length=512)],
        code: Annotated[str, Query(max_length=4096)] = "",
        error: Annotated[str, Query(max_length=100)] = "",
    ) -> RedirectResponse:
        services: Services = request.app.state.services
        oauth = services.ga4_oauth
        if oauth is None:
            raise Ga4OAuthError("ga4_user_oauth_disabled")
        result = "error"
        error_code = "ga4_oauth_denied" if error else ""
        if error:
            try:
                await oauth.cancel(state=state, provider_error=error)
            except Ga4OAuthError as callback_error:
                error_code = callback_error.code
        else:
            try:
                await oauth.callback(state=state, code=code)
                result = "connected"
            except Ga4OAuthError as callback_error:
                error_code = callback_error.code
        query = {"ga4": result}
        if error_code:
            query["code"] = error_code
        separator = "&" if "?" in services.settings.ga4_oauth_return_url else "?"
        return RedirectResponse(
            services.settings.ga4_oauth_return_url
            + separator
            + urllib.parse.urlencode(query),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @application.post("/api/integrations/ga4/disconnect", status_code=204)
    async def disconnect_ga4(identity: Identity, request: Request) -> None:
        services: Services = request.app.state.services
        if services.ga4_oauth is None:
            raise Ga4OAuthError("ga4_user_oauth_disabled")
        await services.ga4_oauth.disconnect(identity.uid)

    @application.get(
        "/api/integrations/ga4/properties",
        response_model=list[Ga4PropertyCandidate],
    )
    async def discover_ga4_properties(
        identity: Identity,
        request: Request,
    ) -> list[Ga4PropertyCandidate]:
        services: Services = request.app.state.services
        discovery = services.ga4_discovery
        if discovery is None or not services.settings.enable_ga4_user_oauth:
            raise Ga4DiscoveryError("ga4_user_oauth_disabled")
        return await discovery.discover(
            user_id=identity.uid,
            request_id=request_id_context.get(),
        )

    async def registration_call(
        request: Request,
        identity: VerifiedIdentity,
        company_id: str,
        operation: str,
        invoke: Callable[[RegistrationService], Awaitable[RegistrationResult]],
    ) -> RegistrationResult:
        services: Services = request.app.state.services
        try:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", company_id):
                raise RegistrationError("invalid_company_id", 422)
            if (
                services.registrations is None
                or not services.settings.enable_admin_connection_checks
            ):
                raise RegistrationError("admin_connection_checks_disabled", 503)
            return await invoke(services.registrations)
        except (RegistrationError, AccessDenied) as error:
            code = (
                error.code
                if isinstance(error, RegistrationError)
                else error.internal_reason
            )
            await services.repository.write_audit_event(
                AuditEvent(
                    id=f"audit_{uuid.uuid4().hex}",
                    request_id=request_id_context.get(),
                    action=f"admin.registration.{operation}",
                    user_id=identity.uid,
                    company_id=company_id,
                    status="denied",
                    details={"error_code": code},
                )
            )
            if isinstance(error, AccessDenied):
                raise
            raise HTTPException(
                status_code=error.status_code, detail={"code": error.code}
            ) from error

    @application.get("/api/admin/companies")
    async def admin_companies(
        identity: Identity, request: Request
    ) -> list[dict[str, str]]:
        services: Services = request.app.state.services
        if (
            services.registrations is None
            or not services.settings.enable_admin_connection_checks
        ):
            raise HTTPException(
                503, detail={"code": "admin_connection_checks_disabled"}
            )
        return await services.registrations.companies(identity.uid)

    @application.get("/api/admin/companies/{company_id}/data-source-inventory")
    async def registration_inventory(
        company_id: str, identity: Identity, request: Request, after: str = ""
    ) -> dict[str, object]:
        return await registration_call(
            request,
            identity,
            company_id,
            "inventory",
            lambda service: service.inventory(identity.uid, company_id, after),
        )

    @application.post(
        "/api/admin/companies/{company_id}/data-source-registrations/{source_id}/legacy"
    )
    async def stage_legacy_registration(
        company_id: str,
        source_id: str,
        payload: LegacyRegistrationRequest,
        identity: Identity,
        request: Request,
    ) -> SourceRegistration:
        return await registration_call(
            request,
            identity,
            company_id,
            "legacy_staged",
            lambda service: service.stage_legacy(
                identity.uid, company_id, source_id, payload, request_id_context.get()
            ),
        )

    @application.post(
        "/api/admin/companies/{company_id}/data-source-registrations/{source_id}/disable"
    )
    async def disable_registration(
        company_id: str,
        source_id: str,
        payload: DisableRegistrationRequest,
        identity: Identity,
        request: Request,
    ) -> SourceRegistration:
        return await registration_call(
            request,
            identity,
            company_id,
            "disabled",
            lambda service: service.disable(
                identity.uid, company_id, source_id, payload, request_id_context.get()
            ),
        )

    @application.post(
        "/api/admin/companies/{company_id}/data-source-registrations", status_code=201
    )
    async def register_source(
        company_id: str,
        payload: RegistrationInput,
        identity: Identity,
        request: Request,
    ) -> SourceRegistration:
        return await registration_call(
            request,
            identity,
            company_id,
            "create",
            lambda service: service.create(
                identity.uid, company_id, payload, request_id_context.get()
            ),
        )

    @application.get(
        "/api/admin/companies/{company_id}/data-source-registrations/{source_id}"
    )
    async def read_registration(
        company_id: str, source_id: str, identity: Identity, request: Request
    ) -> SourceRegistration:
        return await registration_call(
            request,
            identity,
            company_id,
            "read",
            lambda service: service.read(identity.uid, company_id, source_id),
        )

    @application.put(
        "/api/admin/companies/{company_id}/data-source-registrations/{source_id}"
    )
    async def update_registration(
        company_id: str,
        source_id: str,
        payload: UpdateRegistrationRequest,
        identity: Identity,
        request: Request,
    ) -> SourceRegistration:
        return await registration_call(
            request,
            identity,
            company_id,
            "update",
            lambda service: service.update(
                identity.uid,
                company_id,
                source_id,
                payload.definition,
                payload.expected_revision,
                request_id_context.get(),
            ),
        )

    @application.post(
        "/api/admin/companies/{company_id}/data-source-registrations/{source_id}/verify"
    )
    async def verify_registration(
        company_id: str,
        source_id: str,
        payload: RevisionRequest,
        identity: Identity,
        request: Request,
    ) -> SourceRegistration:
        return await registration_call(
            request,
            identity,
            company_id,
            "verify",
            lambda service: service.verify(
                identity.uid,
                company_id,
                source_id,
                payload.expected_revision,
                request_id_context.get(),
            ),
        )

    @application.post(
        "/api/admin/companies/{company_id}/data-source-registrations/{source_id}/activate"
    )
    async def activate_registration(
        company_id: str,
        source_id: str,
        payload: RevisionRequest,
        identity: Identity,
        request: Request,
    ) -> SourceRegistration:
        return await registration_call(
            request,
            identity,
            company_id,
            "activate",
            lambda service: service.activate(
                identity.uid,
                company_id,
                source_id,
                payload.expected_revision,
                request_id_context.get(),
            ),
        )

    @application.put(
        "/api/admin/companies/{company_id}/data-source-registrations/{source_id}/grant"
    )
    async def grant_registration(
        company_id: str,
        source_id: str,
        payload: GrantRequest,
        identity: Identity,
        request: Request,
    ) -> SourceRegistration:
        return await registration_call(
            request,
            identity,
            company_id,
            "grant",
            lambda service: service.grant(
                identity.uid, company_id, source_id, payload, request_id_context.get()
            ),
        )

    @application.post("/api/ga4/sessions", status_code=410)
    async def retired_ga4_chat_session(
        identity: Identity, request: Request
    ) -> JSONResponse:
        # Stale clients must not turn Google discovery into application permission.
        services: Services = request.app.state.services
        await services.repository.write_audit_event(
            AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=request_id_context.get(),
                action="ga4.chat_session.retired",
                user_id=identity.uid,
                status="denied",
            )
        )
        return JSONResponse(
            status_code=410,
            content={"detail": {"code": "ga4_property_session_creation_retired"}},
        )

    @application.post(
        "/api/data-source-sets/{data_source_set_id}/ga4/verify",
        response_model=Ga4PropertyVerification,
    )
    async def verify_ga4_property(
        data_source_set_id: str,
        identity: Identity,
        request: Request,
    ) -> Ga4PropertyVerification | JSONResponse:
        services: Services = request.app.state.services
        if not services.settings.enable_admin_connection_checks:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": {"code": "admin_connection_checks_disabled"}},
            )
        verification = services.ga4_verification
        if verification is None:
            raise Ga4VerificationError("ga4_property_verification_not_configured")
        return await verification.verify(
            user_id=identity.uid,
            data_source_set_id=data_source_set_id,
            request_id=request_id_context.get(),
        )

    @application.get(
        "/api/sessions/{session_id}/ga4/custom-metadata",
        response_model=Ga4MetadataCatalog,
    )
    async def inspect_ga4_custom_metadata(
        session_id: str,
        identity: Identity,
        request: Request,
    ) -> Ga4MetadataCatalog:
        services: Services = request.app.state.services
        metadata = services.ga4_metadata
        if metadata is None or not services.settings.enable_analytics_tools:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "analytics_tools_disabled"},
            )
        session = await services.repository.get_session(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "session_not_found"},
            )
        scope = await services.authorization.authorize_session(identity.uid, session)
        if scope.member.role not in {
            MemberRole.COMPANY_ADMIN,
            MemberRole.PLATFORM_ADMIN,
        }:
            raise AccessDenied("ga4_metadata_admin_required")
        return await metadata.inspect(user_id=identity.uid, session_id=session_id)

    @application.post(
        "/api/sessions",
        response_model=AnalysisSession,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_session(
        payload: CreateSessionRequest,
        identity: Identity,
        request: Request,
    ) -> AnalysisSession:
        services: Services = request.app.state.services
        scope = await services.authorization.authorize_source_set(
            identity.uid, payload.data_source_set_id
        )
        session_id = f"as_{uuid.uuid4().hex}"
        session = AnalysisSession(
            id=session_id,
            user_id=identity.uid,
            company_id=scope.source_set.company_id,
            data_source_set_id=scope.source_set.id,
            data_access_profile_id=scope.source_set.data_access_profile_id,
            source_scope_fingerprint=source_scope_fingerprint(scope.source_set),
            state=AnalysisState(
                data_source_set_id=scope.source_set.id,
                semantic_version=scope.source_set.semantic_version,
            ),
        )
        await services.repository.create_session(session)
        await services.repository.write_audit_event(
            AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=request_id_context.get(),
                action="analysis_session.create",
                user_id=identity.uid,
                company_id=scope.source_set.company_id,
                data_source_set_id=scope.source_set.id,
                session_id=session.id,
                data_access_profile_id=scope.source_set.data_access_profile_id,
                status="success",
            )
        )
        return session

    @application.get("/api/sessions", response_model=list[AnalysisSession])
    async def list_sessions(
        identity: Identity,
        request: Request,
    ) -> list[AnalysisSession]:
        services: Services = request.app.state.services
        return await services.authorization.list_allowed_sessions(identity.uid)

    @application.get(
        "/api/sessions/{session_id}",
        response_model=AnalysisSession,
    )
    async def get_session(
        session_id: str,
        identity: Identity,
        request: Request,
    ) -> AnalysisSession:
        services: Services = request.app.state.services
        session = await services.repository.get_session(session_id)
        if session is None:
            raise AccessDenied("session_not_found")
        await services.authorization.authorize_session(identity.uid, session)
        return session

    @application.delete(
        "/api/sessions/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def archive_session(
        session_id: str,
        identity: Identity,
        request: Request,
    ) -> None:
        services: Services = request.app.state.services
        session = await services.repository.get_session(session_id)
        if session is None:
            raise AccessDenied("session_not_found")
        await services.authorization.authorize_session(identity.uid, session)
        archived = session.model_copy(
            update={
                "status": SessionStatus.ARCHIVED,
                "revision": session.revision + 1,
                "updated_at": utc_now(),
            }
        )
        updated = await services.repository.update_session(
            archived,
            expected_revision=session.revision,
        )
        if not updated:
            raise AnalysisOrchestrationError("analysis_session_conflict")
        await services.repository.write_audit_event(
            AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=request_id_context.get(),
                action="analysis_session.archive",
                user_id=identity.uid,
                company_id=session.company_id,
                data_source_set_id=session.data_source_set_id,
                session_id=session.id,
                data_access_profile_id=session.data_access_profile_id,
                status="success",
                details={"recoverable": True},
            )
        )

    @application.get(
        "/api/sessions/{session_id}/messages",
        response_model=list[AnalysisMessage],
    )
    async def list_messages(
        session_id: str,
        identity: Identity,
        request: Request,
    ) -> list[AnalysisMessage]:
        services: Services = request.app.state.services
        session = await services.repository.get_session(session_id)
        if session is None:
            raise AccessDenied("session_not_found")
        await services.authorization.authorize_session(identity.uid, session)
        return [
            attributed_message(m)
            for m in await services.repository.list_messages(session_id)
        ]

    @application.get(
        "/api/sessions/{session_id}/history",
        response_model=SessionHistoryResponse,
    )
    async def get_session_history(
        session_id: str,
        identity: Identity,
        request: Request,
    ) -> SessionHistoryResponse:
        services: Services = request.app.state.services
        session = await services.repository.get_session(session_id)
        if session is None:
            raise AccessDenied("session_not_found")
        await services.authorization.authorize_session(identity.uid, session)
        messages = await services.repository.list_messages(session_id)
        if any(
            message.session_id != session.id
            or message.user_id != session.user_id
            or message.company_id != session.company_id
            or message.data_source_set_id != session.data_source_set_id
            for message in messages
        ):
            raise AccessDenied("message_scope_mismatch")
        results = (
            await services.orchestrator.restore_results(
                session=session,
                messages=messages,
            )
            if services.orchestrator is not None
            else None
        )
        return SessionHistoryResponse(
            messages=tuple(attributed_message(m) for m in messages),
            results=results or (),
            latest_result=results[-1] if results else None,
        )

    async def browser_scope(
        services: Services, uid: str, session_id: str
    ) -> tuple[AnalysisSession, str]:
        if (
            not services.settings.enable_browser_qwen
            or not services.settings.enable_analytics_tools
        ):
            raise HTTPException(503, detail={"code": "browser_analysis_disabled"})
        session = await services.repository.get_session(session_id)
        if session is None:
            raise AccessDenied("session_not_found")
        scope = await services.authorization.authorize_session(uid, session)
        if (
            not scope.source_set.ga4
            or scope.source_set.ga4.auth_mode != Ga4AuthMode.USER_OAUTH
        ):
            raise AccessDenied("browser_requires_ga4_oauth_set")
        return session, scope.source_set.ga4.timezone

    @application.get("/api/sessions/{session_id}/browser/context")
    async def browser_context(
        session_id: str, identity: Identity, request: Request
    ) -> dict[str, Any]:
        services: Services = request.app.state.services
        session, timezone = await browser_scope(services, identity.uid, session_id)
        definitions = await services.repository.list_semantic_definitions(
            company_id=session.company_id,
            data_source_set_id=session.data_source_set_id,
            version=session.state.semantic_version,
        )
        return {
            "today": utc_now().astimezone(ZoneInfo(timezone)).date(),
            "revision": session.revision,
            "site_context": site_understanding(
                await services.agent.site(identity.uid, session.data_source_set_id)
            ),
            "skills": get_args(BrowserSkill),
            "terms": [
                {"kind": d.kind, "name": d.canonical_name, "aliases": d.aliases[:4]}
                for d in effective_definitions(definitions, session)
                if d.kind in {"metric", "dimension"} and d.ga4_mapping
            ],
            "state": session.state.model_dump(
                include={
                    "metrics",
                    "dimensions",
                    "period",
                    "comparison_period",
                    "focus",
                    "time_grain",
                    "filters",
                }
            ),
        }

    @application.post(
        "/api/sessions/{session_id}/browser/analyze", response_model=AnalysisResponse
    )
    async def browser_analyze(
        session_id: str,
        payload: BrowserMessageRequest,
        identity: Identity,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> AnalysisResponse:
        services: Services = request.app.state.services
        session, _ = await browser_scope(services, identity.uid, session_id)
        if services.orchestrator is None:
            raise AnalysisOrchestrationError("orchestrator_not_configured")
        return await services.orchestrator.analyze(
            user_id=identity.uid,
            session_id=session.id,
            content=payload.content,
            request_id=request_id_context.get(),
            idempotency_key=validate_idempotency_key(idempotency_key),
            browser_plan=payload.plan,
            expected_revision=payload.revision,
        )

    @application.post(
        "/api/sessions/{session_id}/messages",
        response_model=AnalysisResponse,
    )
    async def post_message(
        session_id: str,
        payload: MessageRequest,
        identity: Identity,
        request: Request,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
    ) -> AnalysisResponse | JSONResponse:
        services: Services = request.app.state.services
        session = await services.repository.get_session(session_id)
        if session is None:
            raise AccessDenied("session_not_found")
        await services.authorization.authorize_session(identity.uid, session)
        if not services.settings.enable_analytics_tools:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "detail": {
                        "code": "analytics_tools_disabled",
                        "message": "Analytics is gated pending acceptance.",
                    }
                },
            )
        if not services.settings.vertex_allowed(identity.uid):
            raise AccessDenied("browser_inference_required")
        orchestrator_service = services.orchestrator
        if orchestrator_service is None:
            raise AnalysisOrchestrationError("orchestrator_not_configured")
        operation_key = validate_idempotency_key(idempotency_key)
        return await orchestrator_service.analyze(
            user_id=identity.uid,
            session_id=session.id,
            content=payload.content,
            request_id=request_id_context.get(),
            idempotency_key=operation_key,
        )

    @application.post(
        "/api/sessions/{session_id}/messages/stream",
        response_model=None,
    )
    async def stream_message(
        session_id: str,
        payload: MessageRequest,
        identity: Identity,
        request: Request,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
    ) -> StreamingResponse | JSONResponse:
        services: Services = request.app.state.services
        session = await services.repository.get_session(session_id)
        if session is None:
            raise AccessDenied("session_not_found")
        await services.authorization.authorize_session(identity.uid, session)
        if not services.settings.enable_analytics_tools:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": {"code": "analytics_tools_disabled"}},
            )
        if not services.settings.vertex_allowed(identity.uid):
            raise AccessDenied("browser_inference_required")
        orchestrator_service = services.orchestrator
        if orchestrator_service is None:
            raise AnalysisOrchestrationError("orchestrator_not_configured")
        request_id = request_id_context.get()
        operation_key = validate_idempotency_key(idempotency_key)

        async def events() -> AsyncIterator[str]:
            yield sse_event(
                "status",
                {"stage": "accepted", "request_id": request_id},
            )
            try:
                response = await orchestrator_service.analyze(
                    user_id=identity.uid,
                    session_id=session.id,
                    content=payload.content,
                    request_id=request_id,
                    idempotency_key=operation_key,
                )
                for finding in response.findings:
                    yield sse_event("finding", finding.model_dump(mode="json"))
                if response.visualization is not None:
                    yield sse_event(
                        "visualization",
                        response.visualization.model_dump(mode="json"),
                    )
                for evidence in response.evidence:
                    yield sse_event("evidence", evidence.model_dump(mode="json"))
                yield sse_event("done", response.model_dump(mode="json"))
            except AccessDenied:
                yield sse_event("error", {"code": "access_denied"})
            except AnalysisOrchestrationError as error:
                yield sse_event("error", {"code": error.code})
            except Exception:
                logger.exception(
                    "unexpected streaming analysis failure",
                    extra={"error_code": "analysis_failed"},
                )
                yield sse_event("error", {"code": "analysis_failed"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @application.get("/api/settings/properties", response_model=PropertyPreferences)
    async def property_preferences(
        identity: Identity, request: Request
    ) -> PropertyPreferences:
        services: Services = request.app.state.services
        return await services.agent.preferences(identity.uid)

    @application.put("/api/settings/properties", response_model=PropertyPreferences)
    async def update_property_preferences(
        payload: PropertyPreferences, identity: Identity, request: Request
    ) -> PropertyPreferences:
        services: Services = request.app.state.services
        return await services.agent.save_preferences(identity.uid, payload)

    @application.get(
        "/api/data-source-sets/{source_id}/site-context", response_model=SiteContext
    )
    async def site_context(
        source_id: str, identity: Identity, request: Request
    ) -> SiteContext:
        services: Services = request.app.state.services
        return await services.agent.site(identity.uid, source_id)

    @application.put(
        "/api/data-source-sets/{source_id}/site-context", response_model=SiteContext
    )
    async def update_site_context(
        source_id: str, payload: SiteContext, identity: Identity, request: Request
    ) -> SiteContext:
        services: Services = request.app.state.services
        return await services.agent.save_site(identity.uid, source_id, payload)

    @application.get("/api/sessions/{session_id}/agent", response_model=AgentWorkspace)
    async def agent_workspace(
        session_id: str, identity: Identity, request: Request
    ) -> AgentWorkspace:
        services: Services = request.app.state.services
        return await services.agent.workspace(identity.uid, session_id)

    @application.get(
        "/api/data-source-sets/{source_id}/memory/agreements",
        response_model=list[Agreement],
    )
    async def memory_agreements(
        source_id: str, request: Request, identity: Identity
    ) -> list[Agreement]:
        services: Services = request.app.state.services
        return await services.agent.memory.agreements(identity.uid, source_id)

    @application.post(
        "/api/data-source-sets/{source_id}/memory/agreements", response_model=Agreement
    )
    async def change_memory_agreement(
        source_id: str, payload: AgreementChange, request: Request, identity: Identity
    ) -> Agreement:
        services: Services = request.app.state.services
        return await services.agent.memory.change_agreement(
            identity.uid, source_id, payload
        )

    @application.get("/api/data-source-sets/{source_id}/memory/search")
    async def search_memory(
        source_id: str,
        request: Request,
        identity: Identity,
        q: Annotated[str, Query(min_length=1, max_length=1000)],
    ) -> dict[str, Any]:
        services: Services = request.app.state.services
        return await services.agent.memory.search(identity.uid, source_id, q)

    @application.get("/api/schedules", response_model=list[ReportSchedule])
    async def list_schedules(
        identity: Identity, request: Request
    ) -> list[ReportSchedule]:
        return await ScheduleService(request.app.state.services.agent).list(
            identity.uid
        )

    @application.post(
        "/api/sessions/{session_id}/working-memory", response_model=AgentWorkspace
    )
    async def change_working_memory(
        session_id: str,
        payload: WorkingMemoryChange,
        request: Request,
        identity: Identity,
    ) -> AgentWorkspace:
        services: Services = request.app.state.services
        return await services.agent.change_working_memory(
            identity.uid, session_id, payload
        )

    @application.post("/api/schedules", response_model=ReportSchedule)
    async def save_schedule(
        payload: ReportSchedule, identity: Identity, request: Request
    ) -> ReportSchedule:
        services: Services = request.app.state.services
        if payload.enabled and not services.settings.vertex_allowed(identity.uid):
            raise AccessDenied("browser_inference_required")
        if (
            payload.enabled
            and services.settings.enable_analytics_tools
            and services.settings.resolved_cache_backend == "memory"
        ):
            raise AnalysisOrchestrationError("scheduled_reports_require_durable_cache")
        return await ScheduleService(request.app.state.services.agent).save(
            identity.uid, payload
        )

    @application.get("/api/sessions/{session_id}/agent/operations/{operation_id}")
    async def agent_operation(
        session_id: str, operation_id: str, identity: Identity, request: Request
    ) -> dict[str, Any]:
        key = validate_idempotency_key(operation_id)
        if not key:
            raise HTTPException(422, detail={"code": "invalid_idempotency_key"})
        services: Services = request.app.state.services
        return await services.agent.operation(identity.uid, session_id, key)

    @application.post("/api/sessions/{session_id}/agent/stream", response_model=None)
    async def agent_stream(
        session_id: str,
        payload: MessageRequest,
        identity: Identity,
        request: Request,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> StreamingResponse:
        services: Services = request.app.state.services
        await services.agent.session(identity.uid, session_id)
        if not services.settings.vertex_allowed(identity.uid):
            raise AccessDenied("browser_inference_required")
        if not services.settings.enable_analytics_tools:
            raise AnalysisOrchestrationError("analytics_tools_disabled")
        key = validate_idempotency_key(idempotency_key) or request_id_context.get()

        async def agent_events() -> AsyncIterator[str]:
            try:
                async for event in services.agent.turn(
                    user_id=identity.uid,
                    session_id=session_id,
                    content=payload.content,
                    operation_id=key,
                ):
                    yield sse_event(event["event"], event["data"])
            except AccessDenied:
                yield sse_event("error", {"code": "access_denied"})
            except AnalysisOrchestrationError as error:
                yield sse_event("error", {"code": error.code})

        return StreamingResponse(
            agent_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
        )

    @application.websocket("/api/sessions/{session_id}/browser/agent")
    async def browser_agent(socket: WebSocket, session_id: str) -> None:
        services: Services = socket.app.state.services
        settings = services.settings
        if (
            not settings.enable_browser_qwen
            or not settings.enable_browser_agent
            or not settings.enable_analytics_tools
            or socket.headers.get("origin") not in settings.cors_origins
        ):
            await socket.close(code=1008)
            return
        await serve_browser_agent(
            socket, session_id, services.token_verifier, services.agent
        )

    return application


app = create_app()
