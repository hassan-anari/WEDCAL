# mypy: disable-error-code="no-untyped-def,no-untyped-call"
"""Only synthetic credentials and stubbed S3 operations; no storage/network access."""

import io
import os
import subprocess
import sys
from unittest.mock import Mock

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.response import StreamingBody  # type: ignore[import-untyped]
from botocore.stub import Stubber  # type: ignore[import-untyped]
from django.core.exceptions import ImproperlyConfigured

from apps.content import storage
from apps.players.avatar_storage import S3AvatarStorage


@pytest.fixture
def synthetic_sdk(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-avatar-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-avatar-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "synthetic-avatar-token")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "unused-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "unused-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    settings.BANNER_S3_BUCKET = "synthetic-private-bucket"
    settings.BANNER_S3_ENDPOINT_URL = "https://s3.example.test"
    settings.BANNER_S3_REGION = "us-east-1"
    settings.BANNER_S3_ACCESS_KEY_ID = "synthetic-banner-key"
    settings.BANNER_S3_SECRET_ACCESS_KEY = "synthetic-banner-secret"
    settings.AVATAR_S3_BUCKET = settings.BANNER_S3_BUCKET
    settings.AVATAR_S3_ENDPOINT_URL = settings.BANNER_S3_ENDPOINT_URL
    return settings


def test_dedicated_banner_credentials_do_not_change_avatar_or_inherit_its_token(synthetic_sdk):
    avatar_before = S3AvatarStorage().client
    banner = storage.client()
    avatar_after = S3AvatarStorage().client
    try:
        banner_credentials = banner._request_signer._credentials.get_frozen_credentials()
        assert banner_credentials.access_key == "synthetic-banner-key"
        assert banner_credentials.secret_key == "synthetic-banner-secret"
        assert banner_credentials.token is None
        for avatar in (avatar_before, avatar_after):
            credentials = avatar._request_signer._credentials.get_frozen_credentials()
            assert credentials.access_key == "synthetic-avatar-key"
            assert credentials.secret_key == "synthetic-avatar-secret"
            assert credentials.token == "synthetic-avatar-token"
        assert os.environ["AWS_ACCESS_KEY_ID"] == "synthetic-avatar-key"
        assert os.environ["AWS_SECRET_ACCESS_KEY"] == "synthetic-avatar-secret"
        assert os.environ["AWS_SESSION_TOKEN"] == "synthetic-avatar-token"
    finally:
        for client in (avatar_before, banner, avatar_after):
            client.close()


def test_empty_pair_omits_all_explicit_credentials_for_sdk_role_chain(settings, monkeypatch):
    settings.BANNER_S3_ACCESS_KEY_ID = ""
    settings.BANNER_S3_SECRET_ACCESS_KEY = ""
    factory = Mock()
    monkeypatch.setattr(boto3, "client", factory)
    assert storage.client() is factory.return_value
    options = factory.call_args.kwargs
    assert not {"aws_access_key_id", "aws_secret_access_key", "aws_session_token"} & options.keys()
    assert options["region_name"] == settings.BANNER_S3_REGION
    assert options["config"].connect_timeout == 3
    assert options["config"].read_timeout == 5


def test_empty_pair_resolves_existing_sdk_environment_credentials(synthetic_sdk):
    synthetic_sdk.BANNER_S3_ACCESS_KEY_ID = ""
    synthetic_sdk.BANNER_S3_SECRET_ACCESS_KEY = ""
    client = storage.client()
    try:
        credentials = client._request_signer._credentials.get_frozen_credentials()
        assert credentials.access_key == "synthetic-avatar-key"
        assert credentials.secret_key == "synthetic-avatar-secret"
        assert credentials.token == "synthetic-avatar-token"
    finally:
        client.close()


@pytest.mark.parametrize("missing", ["BANNER_S3_ACCESS_KEY_ID", "BANNER_S3_SECRET_ACCESS_KEY"])
def test_incomplete_runtime_pair_rejected_before_sdk_fallback(synthetic_sdk, monkeypatch, missing):
    setattr(synthetic_sdk, missing, "")
    factory = Mock()
    monkeypatch.setattr(boto3, "client", factory)
    with pytest.raises(ImproperlyConfigured, match="must both be set or both empty"):
        storage.client()
    factory.assert_not_called()


@pytest.mark.parametrize(
    ("access", "secret", "valid"),
    [
        ("", "", True),
        ("synthetic-key", "synthetic-secret", True),
        ("synthetic-key", "", False),
        ("", "synthetic-secret", False),
    ],
)
def test_settings_startup_rejects_incomplete_pair_without_echoing_secrets(access, secret, valid):
    result = subprocess.run(
        [sys.executable, "-c", "import dexo_land.settings.base"],
        env={
            **os.environ,
            "BANNER_S3_ACCESS_KEY_ID": access,
            "BANNER_S3_SECRET_ACCESS_KEY": secret,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is valid
    assert "synthetic-key" not in result.stderr and "synthetic-secret" not in result.stderr
    if not valid:
        assert "ImproperlyConfigured" in result.stderr
        assert "must both be set or both empty" in result.stderr


def test_dedicated_client_preserves_private_encrypted_put_and_bounded_get(
    synthetic_sdk, monkeypatch
):
    client = storage.client()
    monkeypatch.setattr(storage, "client", lambda: client)
    key, content = "banners/synthetic-digest.webp", b"synthetic-encoded-image"
    body = StreamingBody(io.BytesIO(content), len(content))
    try:
        with Stubber(client) as stub:
            stub.add_response(
                "put_object",
                {},
                {
                    "Bucket": "synthetic-private-bucket",
                    "Key": key,
                    "Body": content,
                    "ContentType": "image/webp",
                    "ServerSideEncryption": "AES256",
                    "CacheControl": "public, max-age=31536000, immutable",
                },
            )
            stub.add_response(
                "get_object",
                {"Body": body},
                {
                    "Bucket": "synthetic-private-bucket",
                    "Key": key,
                },
            )
            storage.put(key, content)
            assert storage.get(key) == content
            assert body._raw_stream.closed
            stub.assert_no_pending_responses()
    finally:
        client.close()
