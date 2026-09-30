"""Session tokens. Only the SHA-256 of a token is ever stored."""

import hashlib
import secrets

TOKEN_BYTES = 32

# Longest cookie value worth hashing; real tokens are 43 characters.
MAX_TOKEN_LENGTH = 128


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()
