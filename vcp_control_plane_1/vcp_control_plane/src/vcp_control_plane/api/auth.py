"""Bearer-token authentication dependency (single shared token model).

Matches the auth style of the existing vcp_fleet_simulator gateway_api.py.
When ``require_auth=False`` in Settings, all requests are let through.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from vcp_control_plane.config.settings import Settings, get_settings

# Registered as an OpenAPI security scheme so Swagger UI shows the
# "Authorize" button and actually sends the Authorization header
# (Swagger ignores plain header parameters named "Authorization").
_bearer = HTTPBearer(auto_error=False)


def make_auth_dependency(settings: Settings | None = None):
    """Return a FastAPI dependency that validates the bearer token."""
    cfg = settings or get_settings()

    async def _check(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> None:
        if not cfg.require_auth:
            return
        if not cfg.api_token:
            return  # No token configured → open (warn via startup log)
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or malformed Authorization header (expected: Bearer <token>)",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = credentials.credentials
        if token != cfg.api_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return _check


# Module-level default dependency instance (used in router Depends())
auth_required = make_auth_dependency()
