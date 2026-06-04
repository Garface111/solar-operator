"""Adapter registry. Add new providers here."""
from . import gmp, vec

ADAPTERS = {
    "gmp": gmp,
    "vec": vec,
}

def get_adapter(provider: str):
    if provider not in ADAPTERS:
        raise ValueError(f"Unknown provider: {provider}")
    return ADAPTERS[provider]
