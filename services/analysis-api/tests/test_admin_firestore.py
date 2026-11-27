"""Firestore transaction contract tests, not an emulator/live acceptance claim."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from analytics_ai.admin.models import (
    GrantRequest,
    LegacyRegistrationRequest,
    RegistrationError,
    RegistrationInput,
)
from analytics_ai.admin.policy import fingerprint, grants_fingerprint
from analytics_ai.admin.service import RegistrationService
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.domain.models import (
    Company,
    CompanyMember,
    DataSourceSet,
    MemberRole,
    User,
    UserAccessGrant,
)
from analytics_ai.repositories.firestore import FirestoreRepository
from tests.test_admin_registration import DEFINITION, Verifier
from tests.test_agent_store import assert_firestore_values


class Reference:
    def __init__(self, client, path):
        self.client, self.path = client, path
        self.filters = []
        self.maximum = 100

    def where(self, *, filter):
        self.filters.append(filter)
        return self

    def limit(self, maximum):
        self.maximum = maximum
        return self

    def collection(self, name):
        return Reference(self.client, f"{self.path}/{name}")

    def document(self, name):
        assert "/" not in name
        return Reference(self.client, f"{self.path}/{name}")

    async def get(self, transaction=None):
        if transaction:
            assert not transaction.writes, "all reads must precede writes"
            transaction.reads.append(self.path)
        if self.filters:
            return [
                SimpleNamespace(
                    id=key.split("/")[-1], to_dict=lambda value=payload: deepcopy(value)
                )
                for key, payload in self.client.data.items()
                if key.startswith(self.path + "/")
                and all(payload.get(f.field_path) == f.value for f in self.filters)
            ][: self.maximum]
        value = deepcopy(self.client.data.get(self.path))
        return SimpleNamespace(exists=value is not None, to_dict=lambda: value)


class Transaction:
    def __init__(self, client):
        self.client = client
        self.writes, self.reads = [], []

    def set(self, ref, payload):
        assert_firestore_values(payload)
        self.writes.append(("set", ref.path, deepcopy(payload)))

    def create(self, ref, payload):
        assert_firestore_values(payload)
        self.writes.append(("create", ref.path, deepcopy(payload)))

    def flush(self):
        data = deepcopy(self.client.data)
        for mode, path, payload in self.writes:
            if self.client.fail_audit and path.startswith("auditEvents/"):
                raise RuntimeError("synthetic audit storage failure")
            if mode == "create" and path in data:
                raise RuntimeError("already exists")
            data[path] = payload
        self.client.data = data


class Client:
    def __init__(self):
        self.data = {}
        self.transactions = []
        self.fail_audit = False
        self.before_transaction = None

    def collection(self, name):
        return Reference(self, name)

    def transaction(self):
        if self.before_transaction:
            self.before_transaction()
        transaction = Transaction(self)
        self.transactions.append(transaction)
        return transaction


@pytest.fixture
def stored(monkeypatch):
    client = Client()
    client.data = {
        "companies/company_a": Company(id="company_a", name="A").model_dump(
            mode="json"
        ),
        "users/admin-a": User(uid="admin-a", email="admin@example.com").model_dump(
            mode="json"
        ),
        "users/user-a": User(uid="user-a", email="user@example.com").model_dump(
            mode="json"
        ),
        "companies/company_a/members/admin-a": CompanyMember(
            company_id="company_a", user_id="admin-a", role=MemberRole.COMPANY_ADMIN
        ).model_dump(mode="json"),
        "companies/company_a/members/user-a": CompanyMember(
            company_id="company_a", user_id="user-a"
        ).model_dump(mode="json"),
    }

    def transactional(fn):
        async def run(transaction):
            result = await fn(transaction)
            transaction.flush()
            return result

        return run

    monkeypatch.setattr(
        "analytics_ai.repositories.firestore.firestore.AsyncClient",
        lambda **kwargs: client,
    )
    monkeypatch.setattr(
        "analytics_ai.repositories.firestore.firestore.async_transactional",
        transactional,
    )
    repository = FirestoreRepository("synthetic-project")
    return client, repository, RegistrationService(repository, Verifier())


async def prepare_active(service):
    draft = await service.create(
        "admin-a",
        "company_a",
        RegistrationInput.model_validate(DEFINITION),
        "req-create",
    )
    verified = await service.verify(
        "admin-a", "company_a", draft.source_set.id, 1, "req-verify"
    )
    active = await service.activate(
        "admin-a", "company_a", draft.source_set.id, verified.revision, "req-activate"
    )
    return active


async def test_firestore_roundtrip_activation_and_independent_grant(stored):
    client, repo, service = stored
    active = await prepare_active(service)
    assert (
        await repo.get_registration(active.source_set.id)
    ).model_dump() == active.model_dump()
    assert (await repo.get_data_source_set(active.source_set.id)).enabled
    assert not any(key.startswith("userAccessGrants/") for key in client.data)
    registration = await service.grant(
        "admin-a",
        "company_a",
        active.source_set.id,
        GrantRequest(expected_revision=active.revision, user_id="user-a", enabled=True),
        "req-grant",
    )
    await service.grant(
        "admin-a",
        "company_a",
        active.source_set.id,
        GrantRequest(
            expected_revision=registration.revision, user_id="user-a", enabled=False
        ),
        "req-revoke",
    )
    grants = [v for k, v in client.data.items() if k.startswith("userAccessGrants/")]
    assert len(grants) == 1 and not grants[0]["enabled"]
    for tx in client.transactions:
        assert "users/admin-a" in tx.reads
        assert "companies/company_a" in tx.reads
        assert "companies/company_a/members/admin-a" in tx.reads
        assert any(path.startswith("auditEvents/") for _, path, _ in tx.writes)
    assert "users/user-a" in client.transactions[-1].reads
    assert "companies/company_a/members/user-a" in client.transactions[-1].reads


async def test_audit_failure_leaves_no_registration_or_source(stored):
    client, _, service = stored
    before = deepcopy(client.data)
    client.fail_audit = True
    with pytest.raises(RuntimeError, match="audit storage"):
        await service.create(
            "admin-a",
            "company_a",
            RegistrationInput.model_validate(DEFINITION),
            "req-create",
        )
    assert client.data == before


@pytest.mark.parametrize("audit_failure", [False, True])
async def test_legacy_activation_is_atomic_and_preserves_disabled_grant_documents(
    stored, audit_failure
):
    client, _repo, service = stored
    source = DataSourceSet(
        id="legacy_web",
        company_id="company_a",
        name="旧Web",
        ga4=RegistrationInput.model_validate(DEFINITION).ga4,
        semantic_version="keep",
        enabled=True,
    )
    grant = UserAccessGrant(
        id="legacy_grant", user_id="user-a", data_source_set_id=source.id
    )
    client.data[f"dataSourceSets/{source.id}"] = source.model_dump(mode="json")
    client.data[f"userAccessGrants/{grant.id}"] = grant.model_dump(mode="json")
    client.data["messages/preserved"] = {"content": "existing answer"}
    client.data["semanticDefinitions/preserved"] = {"version": "keep"}
    draft = await service.stage_legacy(
        "admin-a",
        "company_a",
        source.id,
        LegacyRegistrationRequest(
            definition=RegistrationInput.model_validate(DEFINITION),
            source_hash=fingerprint(source),
            grants_hash=grants_fingerprint([grant]),
        ),
        "stage",
    )
    verified = await service.verify(
        "admin-a", "company_a", source.id, draft.revision, "verify"
    )
    before = deepcopy(client.data)
    client.fail_audit = audit_failure
    if audit_failure:
        with pytest.raises(RuntimeError):
            await service.activate(
                "admin-a", "company_a", source.id, verified.revision, "activate"
            )
        assert client.data == before
    else:
        active = await service.activate(
            "admin-a", "company_a", source.id, verified.revision, "activate"
        )
        assert client.data[f"userAccessGrants/{grant.id}"]["enabled"] is False
        assert client.data["messages/preserved"] == before["messages/preserved"]
        assert (
            client.data["semanticDefinitions/preserved"]
            == before["semanticDefinitions/preserved"]
        )
        await service.grant(
            "admin-a",
            "company_a",
            source.id,
            GrantRequest(
                expected_revision=active.revision, user_id="user-a", enabled=True
            ),
            "grant",
        )
        assert client.data[f"userAccessGrants/{grant.id}"]["enabled"] is False
        assert (
            len(
                [
                    g
                    for p, g in client.data.items()
                    if p.startswith("userAccessGrants/") and g["enabled"]
                ]
            )
            == 1
        )


async def test_activation_audit_failure_cannot_publish_scope_or_semantics(stored):
    client, repo, service = stored
    draft = await service.create(
        "admin-a",
        "company_a",
        RegistrationInput.model_validate(DEFINITION),
        "req-create",
    )
    verified = await service.verify(
        "admin-a", "company_a", draft.source_set.id, 1, "req-verify"
    )
    before = deepcopy(client.data)
    client.fail_audit = True
    with pytest.raises(RuntimeError):
        await service.activate(
            "admin-a",
            "company_a",
            draft.source_set.id,
            verified.revision,
            "req-activate",
        )
    assert client.data == before
    assert await repo.get_data_source_set(draft.source_set.id) is None


async def test_role_revocation_between_read_and_write_is_rechecked(stored):
    client, _, service = stored

    def revoke():
        client.data["companies/company_a/members/admin-a"]["role"] = "analyst"

    client.before_transaction = revoke
    with pytest.raises(AccessDenied):
        await service.create(
            "admin-a",
            "company_a",
            RegistrationInput.model_validate(DEFINITION),
            "req-create",
        )
    assert not any(k.startswith("dataSourceRegistrations/") for k in client.data)
    assert not client.transactions[-1].writes


async def test_firestore_stale_revision_and_published_scope_overwrite_are_denied(
    stored,
):
    client, repo, service = stored
    draft = await service.create(
        "admin-a",
        "company_a",
        RegistrationInput.model_validate(DEFINITION),
        "req-create",
    )
    await service.update(
        "admin-a",
        "company_a",
        draft.source_set.id,
        RegistrationInput.model_validate(DEFINITION),
        1,
        "req-update",
    )
    with pytest.raises(RegistrationError, match="revision_conflict"):
        await service.update(
            "admin-a",
            "company_a",
            draft.source_set.id,
            RegistrationInput.model_validate(DEFINITION),
            1,
            "req-update",
        )
    client.data[f"dataSourceSets/{draft.source_set.id}"] = draft.source_set.model_dump(
        mode="json"
    )
    latest = await repo.get_registration(draft.source_set.id)
    with pytest.raises(RegistrationError, match="published_source_conflict"):
        await service.verify(
            "admin-a", "company_a", draft.source_set.id, latest.revision, "req-verify"
        )
