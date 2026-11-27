"""No network: actual metadata verifier, official protobuf contract and credentials."""

from unittest.mock import Mock

import pytest
from google.analytics import admin_v1beta
from google.api_core.exceptions import Forbidden
from google.auth.credentials import AnonymousCredentials

from analytics_ai.admin.models import RegistrationError, RegistrationInput
from analytics_ai.admin.service import RegistrationService
from analytics_ai.admin.verification import Ga4RegistrationVerifier, GoogleStreamReader
from analytics_ai.ga4.credentials import Ga4CredentialError
from analytics_ai.ga4.verification import Ga4VerificationError
from tests.test_admin_registration import DEFINITION
from tests.test_ga4_verification import (
    RecordingCredentials,
    RecordingGa4,
    property_details,
)


class Streams:
    def __init__(self, result=None):
        self.result = result or {
            "properties/222222222/dataStreams/3333333333": "WEB_DATA_STREAM"
        }
        self.calls = []

    async def read(self, source, credentials, verification=None):
        self.calls.append((source, credentials))
        return self.result


def source():
    return RegistrationService._source(
        "dss_test", "company_a", RegistrationInput.model_validate(DEFINITION)
    )


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("name", "properties/999999999", "ga4_registered_property_mismatch"),
        ("time_zone", "Etc/UTC", "ga4_registered_timezone_mismatch"),
        ("currency_code", "USD", "ga4_registered_currency_mismatch"),
    ],
)
async def test_exact_property_metadata_rejects_mismatch(field, value, code):
    metadata = {**property_details(), field: value}
    streams = Streams()
    verifier = Ga4RegistrationVerifier(
        RecordingCredentials(), client=RecordingGa4(metadata), streams=streams
    )
    with pytest.raises(Ga4VerificationError, match=code):
        await verifier.verify(user_id="admin-a", source_set=source(), profile=None)
    assert not streams.calls


@pytest.mark.parametrize(
    "result",
    [
        {"properties/999999999/dataStreams/3333333333": "WEB_DATA_STREAM"},
        {"properties/222222222/dataStreams/3333333333": "IOS_APP_DATA_STREAM"},
        {"properties/222222222/dataStreams/4444444444": "WEB_DATA_STREAM"},
    ],
)
async def test_stream_property_id_and_platform_all_match(result):
    verifier = Ga4RegistrationVerifier(
        RecordingCredentials(),
        client=RecordingGa4(property_details()),
        streams=Streams(result),
    )
    with pytest.raises(RegistrationError, match="registration_stream_mismatch"):
        await verifier.verify(user_id="admin-a", source_set=source(), profile=None)


async def test_metadata_calls_use_selected_request_credential_only():
    credentials = RecordingCredentials()
    ga4, streams = RecordingGa4(property_details()), Streams()
    verifier = Ga4RegistrationVerifier(credentials, client=ga4, streams=streams)
    assert (
        await verifier.verify(user_id="admin-a", source_set=source(), profile=None)
    ).credential_identity == "google-user-a"
    assert credentials.calls == [("admin-a", "dss_test", None)]
    assert ga4.calls == [("get_property_details", {"property_id": "222222222"})]
    assert isinstance(streams.calls[0][1], AnonymousCredentials)


async def test_google_permission_failure_does_not_try_sa():
    credentials = RecordingCredentials()
    ga4, streams = RecordingGa4(Forbidden("denied")), Streams()
    verifier = Ga4RegistrationVerifier(credentials, client=ga4, streams=streams)
    with pytest.raises(Forbidden):
        await verifier.verify(user_id="admin-a", source_set=source(), profile=None)
    assert len(credentials.calls) == 1 and not streams.calls


async def test_missing_oauth_never_calls_metadata():
    class DeniedCredentials:
        async def get_credentials(self, **kwargs):
            raise Ga4CredentialError("ga4_user_connection_required")

    ga4, streams = RecordingGa4(property_details()), Streams()
    verifier = Ga4RegistrationVerifier(DeniedCredentials(), client=ga4, streams=streams)
    with pytest.raises(Ga4CredentialError):
        await verifier.verify(user_id="admin-a", source_set=source(), profile=None)
    assert not ga4.calls and not streams.calls


@pytest.mark.parametrize("wrong_name", [False, True])
async def test_google_stream_reader_uses_exact_get_readonly_bounded_no_retry(
    monkeypatch, wrong_name
):
    name = "properties/222222222/dataStreams/3333333333"
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get_data_stream.return_value = admin_v1beta.DataStream(
        name=name if not wrong_name else "properties/999/dataStreams/3333333333",
        type_=admin_v1beta.DataStream.DataStreamType.WEB_DATA_STREAM,
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(
        "analytics_ai.admin.verification.admin_v1beta.AnalyticsAdminServiceClient",
        factory,
    )
    credentials = AnonymousCredentials()
    reader = GoogleStreamReader()
    if wrong_name:
        with pytest.raises(RegistrationError):
            await reader.read(source(), credentials)
    else:
        assert await reader.read(source(), credentials) == {name: "WEB_DATA_STREAM"}
    factory.assert_called_once_with(credentials=credentials)
    client.get_data_stream.assert_called_once_with(name=name, retry=None, timeout=5)
    client.__exit__.assert_called_once()


@pytest.mark.parametrize(
    "parent", ["properties/777777777", "accounts/777", "properties/222222222"]
)
async def test_subproperty_stream_uses_only_google_confirmed_parent(
    monkeypatch, parent
):
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get_data_stream.return_value = admin_v1beta.DataStream(
        name=f"{parent}/dataStreams/3333333333",
        type_=admin_v1beta.DataStream.DataStreamType.WEB_DATA_STREAM,
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(
        "analytics_ai.admin.verification.admin_v1beta.AnalyticsAdminServiceClient",
        factory,
    )
    metadata = {
        **property_details(),
        "property_type": "PROPERTY_TYPE_SUBPROPERTY",
        "parent": parent,
    }
    verifier = Ga4RegistrationVerifier(
        RecordingCredentials(), client=RecordingGa4(metadata)
    )
    if parent != "properties/777777777":
        with pytest.raises(
            RegistrationError, match="registration_stream_parent_invalid"
        ):
            await verifier.verify(user_id="admin-a", source_set=source(), profile=None)
        assert not client.get_data_stream.called
    else:
        verified = await verifier.verify(
            user_id="admin-a", source_set=source(), profile=None
        )
        assert verified.stream_metadata_parent == parent
        client.get_data_stream.assert_called_once_with(
            name=f"{parent}/dataStreams/3333333333", retry=None, timeout=5
        )
        assert (
            source().ga4.property_id == "222222222"
        )  # report scope is never reparented


async def test_inherited_stream_denial_never_falls_back_to_another_credential(
    monkeypatch,
):
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get_data_stream.side_effect = Forbidden("no source metadata permission")
    monkeypatch.setattr(
        "analytics_ai.admin.verification.admin_v1beta.AnalyticsAdminServiceClient",
        Mock(return_value=client),
    )
    credentials = RecordingCredentials()
    verifier = Ga4RegistrationVerifier(
        credentials,
        client=RecordingGa4(
            {
                **property_details(),
                "property_type": "PROPERTY_TYPE_SUBPROPERTY",
                "parent": "properties/777777777",
            }
        ),
    )
    with pytest.raises(Forbidden):
        await verifier.verify(user_id="admin-a", source_set=source(), profile=None)
    assert len(credentials.calls) == 1
