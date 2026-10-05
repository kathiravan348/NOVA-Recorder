"""Access tokens are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256, D39)."""

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("ascii"))

    def encrypt(self, token: str) -> bytes:
        return self._fernet.encrypt(token.encode("utf-8"))

    def decrypt(self, sealed: bytes) -> str:
        try:
            return self._fernet.decrypt(sealed).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Token cannot be decrypted with this key") from exc


def new_key() -> str:
    """A fresh key for `NOVA_BROKER_TOKEN_KEY`."""
    return Fernet.generate_key().decode("ascii")
