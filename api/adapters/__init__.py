"""Adapter registry. Add new providers here."""
from . import gmp, vec, smarthub
from .smarthub import SMARTHUB_UTILITIES, is_smarthub_provider  # noqa: F401

ADAPTERS: dict[str, object] = {
    "gmp": gmp,
}

# Register every SmartHub utility under its lowercase provider code.
# "vec" is included here — existing UtilityAccount/UtilitySession rows with
# provider="vec" continue to route to the universal smarthub adapter.
for _code, _info in SMARTHUB_UTILITIES.items():
    ADAPTERS[_info["provider"]] = smarthub


def get_adapter(provider: str):
    key = provider.strip().lower()
    if key in ADAPTERS:
        return ADAPTERS[key]
    # Discovered SmartHub utilities ("sh_<subdomain>", minted on first capture
    # from a not-yet-cataloged *.smarthub.coop host) all route to the universal
    # smarthub adapter — that's what makes a brand-new co-op work on first
    # login with zero code changes.
    if key.startswith(smarthub.DISCOVERED_PREFIX):
        return smarthub
    raise ValueError(f"Unknown provider: {provider}")
