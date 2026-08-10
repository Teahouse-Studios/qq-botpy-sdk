from typing import Dict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def derive_ed25519_seed(bot_secret: str) -> bytes:
    """按照 QQ Webhook 规范从 AppSecret 派生 32 字节 Ed25519 seed。"""

    secret = bot_secret.encode("utf-8")
    if not secret:
        raise ValueError("bot_secret must not be empty")
    repeat_count = (32 + len(secret) - 1) // len(secret)
    return (secret * repeat_count)[:32]


def ed25519_sign(bot_secret: str, message: bytes) -> str:
    private_key = Ed25519PrivateKey.from_private_bytes(derive_ed25519_seed(bot_secret))
    return private_key.sign(message).hex()


def verify_webhook_signature(
    *,
    body: bytes,
    timestamp: str,
    signature: str,
    bot_secret: str,
) -> bool:
    try:
        signature_bytes = bytes.fromhex(signature)
        private_key = Ed25519PrivateKey.from_private_bytes(derive_ed25519_seed(bot_secret))
        private_key.public_key().verify(signature_bytes, timestamp.encode("utf-8") + body)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def sign_validation_response(
    *,
    plain_token: str,
    event_ts: str,
    bot_secret: str,
) -> Dict[str, str]:
    message = (event_ts + plain_token).encode("utf-8")
    return {
        "plain_token": plain_token,
        "signature": ed25519_sign(bot_secret, message),
    }
