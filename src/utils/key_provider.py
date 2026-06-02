"""
Key provider. Abstracts where the private key comes from.
In prod you should never be reading a raw key from an env var.

Implement KMSKeyProvider or VaultKeyProvider and wire it in before going live.
The .env fallback is here for testnet / local dev only.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class KeyProvider(ABC):
    @abstractmethod
    async def get_private_key(self) -> str:
        ...


class EnvKeyProvider(KeyProvider):
    """
    Reads key from env var. Testnet/local only.
    If you use this in prod with real capital, that's on you.
    """
    def __init__(self, env_var: str = "PRIVATE_KEY") -> None:
        self._var = env_var

    async def get_private_key(self) -> str:
        key = os.getenv(self._var)
        if not key:
            raise RuntimeError(
                f"${self._var} not set. "
                "For prod, implement KMSKeyProvider or VaultKeyProvider."
            )
        if not key.startswith("0x"):
            key = "0x" + key
        log.warning("using env var key provider — NOT for production")
        return key


class KMSKeyProvider(KeyProvider):
    """
    AWS KMS signing. Generates signatures on-chain without the key ever
    leaving KMS. Requires kms:Sign permission and a secp256k1 key.

    TODO: implement — requires boto3 + custom signing logic for EIP-155 txs.
    Reference: https://luhenning.medium.com/the-dark-side-of-the-elliptic-curve-signing-ethereum-transactions-with-aws-kms-in-javascript-83610d9a6f81
    """
    def __init__(self, key_id: str) -> None:
        self._key_id = key_id

    async def get_private_key(self) -> str:
        raise NotImplementedError(
            "KMS signing not yet implemented. "
            "Use EnvKeyProvider for testnet or implement KMS signing."
        )


class VaultKeyProvider(KeyProvider):
    """
    HashiCorp Vault KV secret.
    TODO: implement using hvac or vault SDK.
    """
    def __init__(self, vault_addr: str, secret_path: str) -> None:
        self._addr = vault_addr
        self._path = secret_path

    async def get_private_key(self) -> str:
        raise NotImplementedError("Vault provider not yet implemented")


def key_provider_from_env() -> KeyProvider:
    """Factory — picks the right provider based on env config."""
    if os.getenv("KMS_KEY_ID"):
        return KMSKeyProvider(os.environ["KMS_KEY_ID"])
    if os.getenv("VAULT_ADDR"):
        return VaultKeyProvider(
            os.environ["VAULT_ADDR"],
            os.getenv("VAULT_SECRET_PATH", "secret/lp-engine/pk"),
        )
    return EnvKeyProvider()
