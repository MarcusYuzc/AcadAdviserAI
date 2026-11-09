"""Server-side sign-in. The browser signs in with Cognito; only a verified ID token is trusted."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt

_GMAIL = {"gmail.com", "googlemail.com"}


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str


class InvalidToken(Exception):
    pass


def email_key(email: str) -> str:
    """One free search per mailbox: jane+1@gmail.com and j.ane@gmail.com share a key."""
    local, _, domain = email.strip().lower().rpartition("@")
    local = local.split("+", 1)[0]
    if domain in _GMAIL:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


class CognitoVerifier:
    def __init__(self, region: str, user_pool_id: str, client_id: str, jwks_client: Any = None):
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        self.client_id = client_id
        self._jwks = jwks_client or jwt.PyJWKClient(
            f"{self.issuer}/.well-known/jwks.json", cache_keys=True, lifespan=3600
        )

    def verify(self, token: str) -> Identity:
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=self.client_id, issuer=self.issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(type(exc).__name__) from exc
        # Access tokens carry no email, and an unverified email could claim someone's free search.
        if claims.get("token_use") != "id" or claims.get("email_verified") not in (True, "true"):
            raise InvalidToken("not a verified ID token")
        if not claims.get("email"):
            raise InvalidToken("no email")
        return Identity(sub=claims["sub"], email=claims["email"])
