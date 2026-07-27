"""Services transverses."""

from __future__ import annotations

import hashlib
from typing import Any

from django.http import HttpRequest

from .models import AuditLog


def record_audit(
    action: str,
    *,
    actor=None,
    obj=None,
    summary: str = "",
    request: HttpRequest | None = None,
    **metadata: Any,
) -> AuditLog:
    """Enregistre une entree d'audit. A appeler pour toute decision IA."""
    object_type = obj.__class__.__name__ if obj is not None else ""
    object_id = str(getattr(obj, "pk", "")) if obj is not None else ""

    ip = None
    if request is not None:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        ip = forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR")
        if actor is None and getattr(request, "user", None) and request.user.is_authenticated:
            actor = request.user

    return AuditLog.objects.create(
        actor=actor,
        action=action,
        object_type=object_type,
        object_id=object_id,
        summary=summary[:255],
        metadata=metadata,
        ip_address=ip,
    )


def sha256_of(fileobj) -> str:
    """Hache un fichier par blocs. Sert de cle de cache : meme CV => zero recalcul."""
    digest = hashlib.sha256()
    for chunk in iter(lambda: fileobj.read(8192), b""):
        digest.update(chunk)
    fileobj.seek(0)
    return digest.hexdigest()
