"""Headless fingerprint hardening.

Portals increasingly gate on trivial headless tells (navigator.webdriver, a
missing plugins array, HeadlessChrome in the UA). None of this is adversarial
evasion — it's making an automated login look like the ordinary desktop Chrome
the customer would use themselves, which is what a hands-off refresh IS. The
heavy lifting for reliability is session REUSE (log in rarely) and a clean
egress IP; this just removes the cheap giveaways.
"""
from __future__ import annotations

from . import config

# Injected before any page script runs. Kept minimal and stable.
_INIT_SCRIPT = """
(() => {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
  try {
    if (!navigator.languages || !navigator.languages.length)
      Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  } catch (e) {}
  try {
    if (!(navigator.plugins && navigator.plugins.length))
      Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
  } catch (e) {}
  try {
    const orig = window.navigator.permissions && window.navigator.permissions.query;
    if (orig) window.navigator.permissions.query = (p) =>
      p && p.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : orig(p);
  } catch (e) {}
})();
"""


def context_options(storage_state: dict | None = None) -> dict:
    """kwargs for browser.new_context() — a believable desktop profile."""
    opts: dict = {
        "user_agent": config.user_agent(),
        "viewport": {"width": 1366, "height": 900},
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": False,
        # Utility portals are US-East; a plausible geolocation avoids some checks.
        "ignore_https_errors": False,
    }
    if storage_state:
        opts["storage_state"] = storage_state
    proxy = config.proxy_url()
    if proxy:
        opts["proxy"] = {"server": proxy}
    return opts


async def apply(context) -> None:
    """Install the init script on every page in the context."""
    await context.add_init_script(_INIT_SCRIPT)


def launch_args() -> list[str]:
    """Chromium flags. --disable-blink-features=AutomationControlled removes the
    most common automation banner; the rest are standard container-safe flags."""
    return [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]
