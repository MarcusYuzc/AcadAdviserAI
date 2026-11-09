from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from acadadviser.auth import CognitoVerifier, InvalidToken, email_key

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class StubJwks:
    def get_signing_key_from_jwt(self, token):
        return jwt.PyJWK.from_dict(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True))


def token(key=KEY, **overrides):
    now = int(time.time())
    claims = {
        "sub": "user-1", "email": "alice@example.com", "email_verified": True, "token_use": "id",
        "aud": "client-1", "iss": ISSUER, "iat": now, "exp": now + 3600, **overrides,
    }
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


@pytest.fixture
def verifier():
    return CognitoVerifier("us-east-1", "us-east-1_pool", "client-1", jwks_client=StubJwks())


def test_accepts_a_verified_id_token(verifier):
    identity = verifier.verify(token())
    assert (identity.sub, identity.email) == ("user-1", "alice@example.com")


@pytest.mark.parametrize("bad", [
    {"aud": "another-client"},
    {"iss": "https://cognito-idp.us-east-1.amazonaws.com/other_pool"},
    {"exp": int(time.time()) - 10},
    {"token_use": "access"},
    {"email_verified": False},
    {"email": None},
])
def test_rejects_tokens_that_are_not_ours_or_not_verified(verifier, bad):
    with pytest.raises(InvalidToken):
        verifier.verify(token(**bad))


def test_rejects_a_forged_signature(verifier):
    with pytest.raises(InvalidToken):
        verifier.verify(token(key=OTHER_KEY))


def test_email_key_folds_aliases():
    assert email_key("J.Ane+promo@Gmail.com") == email_key("jane@googlemail.com") == "jane@gmail.com"
    assert email_key("first.last+x@example.com") == "first.last@example.com"
