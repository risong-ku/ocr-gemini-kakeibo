"""Shared-passcode verification and stateless signed-cookie request policy."""

import hashlib
import secrets
from collections.abc import Callable

from itsdangerous import BadData, URLSafeTimedSerializer
from starlette import status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp

from kakeibo.config import Settings

SESSION_COOKIE_NAME = "session"
SESSION_MAX_AGE = 90 * 24 * 60 * 60
PASSCODE_FINGERPRINT_LENGTH = 8
PUBLIC_PATHS = frozenset({"/healthz", "/login"})
STATIC_PREFIX = "/static/"
API_PREFIX = "/api/"


class SessionAuth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.serializer = URLSafeTimedSerializer(settings.session_secret)
        self.fingerprint = hashlib.sha256(settings.app_passcode.encode()).hexdigest()[
            :PASSCODE_FINGERPRINT_LENGTH
        ]

    def verify_passcode(self, passcode: str) -> bool:
        # Bytes support Unicode passphrases without compare_digest's ASCII limit.
        return secrets.compare_digest(
            passcode.encode(), self.settings.app_passcode.encode()
        )

    def create_token(self) -> str:
        return self.serializer.dumps({"auth": True, "pv": self.fingerprint})

    def verify_token(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            payload = self.serializer.loads(token, max_age=SESSION_MAX_AGE)
        except BadData:
            return False
        if not isinstance(payload, dict) or payload.get("auth") is not True:
            return False
        fingerprint = payload.get("pv")
        if not isinstance(fingerprint, str):
            return False
        return secrets.compare_digest(fingerprint.encode(), self.fingerprint.encode())

    def set_cookie(self, response: Response) -> None:
        response.set_cookie(
            SESSION_COOKIE_NAME,
            self.create_token(),
            max_age=SESSION_MAX_AGE,
            path="/",
            secure=self.settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self, app: ASGIApp, auth: SessionAuth | Callable[[], SessionAuth]
    ) -> None:
        super().__init__(app)
        self.auth = auth

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(STATIC_PREFIX):
            return await call_next(request)
        auth = self.auth() if callable(self.auth) else self.auth
        if auth.verify_token(request.cookies.get(SESSION_COOKIE_NAME)):
            return await call_next(request)
        if path.startswith(API_PREFIX):
            return JSONResponse(
                {"detail": "not authenticated"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
