"""Validación de la firma HMAC que envía Meta en cada webhook."""

from __future__ import annotations

import hashlib
import hmac


def valid_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Comprueba `X-Hub-Signature-256`.

    Si no hay `WA_APP_SECRET` configurado se acepta todo: es cómodo para
    desarrollo local, pero en producción debes configurarlo.
    """
    if not app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False

    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    received = header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)
