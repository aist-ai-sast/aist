from __future__ import annotations

from dojo.utils import dojo_crypto_encrypt, prepare_for_view


def encrypt_action_secret_config(secret_config: dict) -> dict:
    encrypted = {}
    for key, value in (secret_config or {}).items():
        if value:
            encrypted_value = dojo_crypto_encrypt(str(value))
            if encrypted_value:
                encrypted[key] = encrypted_value
    return encrypted


def decrypt_action_secret_config(secret_config: dict) -> dict:
    decrypted = {}
    for key, value in (secret_config or {}).items():
        if isinstance(value, str) and value.startswith("AES.1:"):
            decrypted[key] = prepare_for_view(value)
        elif value:
            decrypted[key] = value
    return decrypted
