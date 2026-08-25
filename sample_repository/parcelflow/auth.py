import hashlib
import hmac
import time


class TokenVerifier:
    """Verifies signed expiring service tokens without logging token material."""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def verify(self, subject: str, expires_at: int, signature: str) -> bool:
        if expires_at <= int(time.time()):
            return False
        payload = f"{subject}:{expires_at}".encode()
        expected = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
