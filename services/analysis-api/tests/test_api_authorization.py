from __future__ import annotations

from fastapi.testclient import TestClient

from analytics_ai.api.app import create_app
from analytics_ai.auth.firebase import VerifiedIdentity
from analytics_ai.config import Settings
from analytics_ai.repositories.memory import InMemoryRepository


class StaticVerifier:
    async def verify(self, token: str) -> VerifiedIdentity:
        return VerifiedIdentity(uid=token, email=f"{token}@example.com")


def client(repository: InMemoryRepository) -> TestClient:
    app = create_app(
        Settings(environment="test"),
        repository=repository,
        token_verifier=StaticVerifier(),
    )
    return TestClient(app)


def auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_id}"}


def test_health_and_legacy_local_healthz_require_no_authentication() -> None:
    api = client(InMemoryRepository())
    for path in ("/health", "/healthz"):
        response = api.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_settings_put_preflight_and_authenticated_save(
    repository: InMemoryRepository,
) -> None:
    app = client(repository)
    origin = "http://localhost:5173"
    for path, body in (
        ("/api/settings/properties", {"hidden_property_ids": ["222222222"]}),
        (
            "/api/data-source-sets/company_a_web/site-context",
            {"site_basics": "情報サイト"},
        ),
    ):
        preflight = app.options(
            path,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin
        assert "PUT" in preflight.headers["access-control-allow-methods"]
        assert app.put(path, json=body, headers={"Origin": origin}).status_code == 401
        response = app.put(
            path, json=body, headers={**auth("user-a"), "Origin": origin}
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert response.json()["revision"] == 1


def test_settings_preflight_still_rejects_unapproved_origin() -> None:
    response = client(InMemoryRepository()).options(
        "/api/settings/properties",
        headers={
            "Origin": "https://unapproved.example",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_authenticated_user_can_read_only_their_verified_identity() -> None:
    response = client(InMemoryRepository()).get(
        "/api/me",
        headers=auth("firebase-user"),
    )
    assert response.status_code == 200
    assert response.json() == {
        "uid": "firebase-user",
        "email": "firebase-user@example.com",
    }


def test_identity_diagnostic_requires_authentication() -> None:
    response = client(InMemoryRepository()).get("/api/me")
    assert response.status_code == 401
    assert response.json() == {"detail": {"code": "authentication_required"}}


def test_user_sees_only_explicitly_granted_source_set(
    repository: InMemoryRepository,
) -> None:
    response = client(repository).get(
        "/api/data-source-sets",
        headers=auth("user-a"),
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["company_a_web"]
    assert response.json()[0]["ga4_property_id"] == "222222222"


def test_backend_rejects_same_company_ungranted_set(
    repository: InMemoryRepository,
) -> None:
    response = client(repository).post(
        "/api/sessions",
        headers=auth("user-a"),
        json={"data_source_set_id": "company_a_store"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "access_denied"}}
    assert repository.sessions == {}


def test_session_scope_is_created_from_server_records(
    repository: InMemoryRepository,
) -> None:
    response = client(repository).post(
        "/api/sessions",
        headers=auth("user-a"),
        json={"data_source_set_id": "company_a_web"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["company_id"] == "company_a"
    assert payload["data_source_set_id"] == "company_a_web"
    assert payload["data_access_profile_id"] == "dap_company_a"
    assert payload["state"]["semantic_version"] == "a-web-v1"
    assert len(repository.audit_events) == 1


def test_analytics_endpoint_stays_gated_after_authorization(
    repository: InMemoryRepository,
) -> None:
    api = client(repository)
    session = api.post(
        "/api/sessions",
        headers=auth("user-a"),
        json={"data_source_set_id": "company_a_web"},
    ).json()
    response = api.post(
        f"/api/sessions/{session['id']}/messages",
        headers=auth("user-a"),
        json={"content": "今月のCVは？"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "analytics_tools_disabled"
    assert repository.messages == {}


def test_other_user_cannot_read_session_messages(
    repository: InMemoryRepository,
) -> None:
    api = client(repository)
    session = api.post(
        "/api/sessions",
        headers=auth("user-a"),
        json={"data_source_set_id": "company_a_web"},
    ).json()

    response = api.get(
        f"/api/sessions/{session['id']}/messages",
        headers=auth("user-b"),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "access_denied"}}
    history = api.get(
        f"/api/sessions/{session['id']}/history",
        headers=auth("user-b"),
    )
    assert history.status_code == 403
    assert history.json() == {"detail": {"code": "access_denied"}}


def test_session_owner_can_archive_session_and_it_is_no_longer_accessible(
    repository: InMemoryRepository,
) -> None:
    api = client(repository)
    session = api.post(
        "/api/sessions",
        headers=auth("user-a"),
        json={"data_source_set_id": "company_a_web"},
    ).json()

    response = api.delete(
        f"/api/sessions/{session['id']}",
        headers=auth("user-a"),
    )

    assert response.status_code == 204
    assert repository.sessions[session["id"]].status.value == "archived"
    assert api.get("/api/sessions", headers=auth("user-a")).json() == []
    assert (
        api.get(
            f"/api/sessions/{session['id']}/messages",
            headers=auth("user-a"),
        ).status_code
        == 403
    )
    archive_audit = repository.audit_events[-1]
    assert archive_audit.action == "analysis_session.archive"
    assert archive_audit.session_id == session["id"]
    assert archive_audit.details == {"recoverable": True}


def test_other_user_cannot_archive_session(
    repository: InMemoryRepository,
) -> None:
    api = client(repository)
    session = api.post(
        "/api/sessions",
        headers=auth("user-a"),
        json={"data_source_set_id": "company_a_web"},
    ).json()

    response = api.delete(
        f"/api/sessions/{session['id']}",
        headers=auth("user-b"),
    )

    assert response.status_code == 403
    assert repository.sessions[session["id"]].status.value == "active"


def test_streaming_endpoint_stays_gated_without_persisting_message(
    repository: InMemoryRepository,
) -> None:
    api = client(repository)
    session = api.post(
        "/api/sessions",
        headers=auth("user-a"),
        json={"data_source_set_id": "company_a_web"},
    ).json()

    response = api.post(
        f"/api/sessions/{session['id']}/messages/stream",
        headers=auth("user-a"),
        json={"content": "今月のUUは？"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "analytics_tools_disabled"
    assert repository.messages == {}
