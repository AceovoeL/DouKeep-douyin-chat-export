"""Verified TLS using native OS roots and the public CA bundle."""
import os
import ssl
from functools import lru_cache

import certifi
import truststore


@lru_cache(maxsize=1)
def client_context():
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=certifi.where())
    # Explicit CA bundles are additive; never disable hostname/certificate checks.
    if os.environ.get('SSL_CERT_FILE'):
        context.load_verify_locations(cafile=os.environ['SSL_CERT_FILE'])
    if os.environ.get('SSL_CERT_DIR'):
        context.load_verify_locations(capath=os.environ['SSL_CERT_DIR'])
    return context
