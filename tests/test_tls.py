import ssl
from common.tls import client_context


def test_media_context_verifies_hostname_and_certificate():
    client_context.cache_clear()
    ctx=client_context()
    assert ctx.verify_mode==ssl.CERT_REQUIRED
    assert ctx.check_hostname


def test_additional_ca_file_must_exist(monkeypatch):
    monkeypatch.setenv('SSL_CERT_FILE','/nonexistent/issue37-ca.pem')
    client_context.cache_clear()
    try:
        import pytest
        with pytest.raises(OSError):client_context()
    finally:client_context.cache_clear()
