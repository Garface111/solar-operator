"""Release Playwright API response buffers immediately after consumption."""
from contextlib import asynccontextmanager
import logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def disposing_response(response):
    try:
        yield response
    finally:
        try:
            await response.dispose()
        except Exception as exc:
            # Context shutdown remains final cleanup. Preserve valid captures.
            log.warning("capture response disposal failed: %s", type(exc).__name__)
