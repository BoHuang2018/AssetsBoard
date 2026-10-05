"""AssetsBoard (AB) — multi-venue investment dashboard (read-only).

Tracks CEX (Bitget, MEXC, Kraken, Crypto.com App), IBKR (local Gateway + Flex),
and public on-chain wallets. Only signed read / public RPC calls — no trading,
withdrawals, or private keys for wallets.
"""
__version__ = "0.2.0"

# python.org builds on macOS ship without a CA bundle; use certifi when the system default has none.
import os as _os
if not _os.environ.get("SSL_CERT_FILE"):
    try:
        import ssl as _ssl, certifi as _certifi
        _paths = _ssl.get_default_verify_paths()
        if not (_paths.cafile and _os.path.exists(_paths.cafile)):
            _os.environ["SSL_CERT_FILE"] = _certifi.where()
    except Exception:
        pass
