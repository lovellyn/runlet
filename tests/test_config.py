import pytest

from runlet.config import Settings


@pytest.mark.parametrize(
    "url",
    [
        "ftp://files.example.com",
        "https://user:pass@files.example.com",
        "https://files.example.com/?token=x",
        "https://files.example.com/#x",
        "https://files.example.com/?",
        "https://files.example.com/#",
        "https://files.example.com/\\path",
        "https://files.example.com/with space",
        "https://",
    ],
)
def test_public_base_url_rejects_invalid_values(url):
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        Settings(token="example", public_base_url=url)


def test_export_settings_from_environment(monkeypatch):
    monkeypatch.setenv("SANDBOX_TOKEN", "example")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://files.example.com")
    monkeypatch.setenv("EXPORT_TTL_SECONDS", "60")
    monkeypatch.setenv("MAX_EXPORT_BYTES", "1024")
    monkeypatch.setenv("MAX_EXPORTS", "2")
    settings = Settings.from_env()
    assert (settings.export_ttl_seconds, settings.max_export_bytes, settings.max_exports) == (
        60,
        1024,
        2,
    )
    assert settings.public_base_url == "https://files.example.com"


def test_public_hosts_and_origins_require_explicit_configuration(monkeypatch):
    monkeypatch.setenv("SANDBOX_TOKEN", "test-token")
    monkeypatch.setenv("SPACE_HOST", "implicit-provider.example")
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    settings = Settings.from_env()
    assert "implicit-provider.example" not in settings.allowed_hosts
    assert "https://implicit-provider.example" not in settings.allowed_origins
    monkeypatch.setenv("ALLOWED_HOSTS", " sandbox.example.com, sandbox.example.com:8443 ")
    monkeypatch.setenv("ALLOWED_ORIGINS", " https://sandbox.example.com ")
    configured = Settings.from_env()
    assert set(settings.allowed_hosts) < set(configured.allowed_hosts)
    assert "sandbox.example.com:8443" in configured.allowed_hosts
    assert "https://sandbox.example.com" in configured.allowed_origins
