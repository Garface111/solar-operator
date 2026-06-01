"""Adapter registry. Add new providers here."""
from . import gmp

ADAPTERS = {
    "gmp": gmp,
}

def get_adapter(provider: str):
    if provider not in ADAPTERS:
        raise ValueError(f"Unknown provider: {provider}")
    return ADAPTERS[provider]
