"""Phone access: a public HTTPS door to the portal, and a link that finds you.

The portal binds loopback on purpose — a single-shared-password view of the
household's entire financial life has no business listening on the LAN. To use
it from a phone we put a Cloudflare tunnel in front instead: TLS terminated by
Cloudflare, nothing new listening on the network, and the origin still only
reachable at 127.0.0.1.

The wart in the free "quick tunnel" is that its hostname is random and changes
every time the tunnel restarts, which makes a phone bookmark useless. So this
module watches for the change and emails the household the new link — the link
lives in your inbox, where a phone already looks. A stable hostname would need
`cloudflared tunnel login` in a browser (see notes in DEPLOY-FORDBRAIN.md);
until someone does that, this is the honest substitute.

Security posture, stated plainly because it is a real tradeoff: the URL is
unguessable but it is not a secret, so the app's password is what actually
stands between the internet and the household's finances. Login throttling and
the HttpOnly/Secure/SameSite session cookie are therefore load-bearing here in
a way they were not on loopback.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("bankai.tunnel")

URL_FILE = Path("/root/bankai-data/tunnel-url")
#: cloudflared prints the assigned hostname once, in a banner, at startup.
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def parse_url(text: str) -> str | None:
    """Pull the tunnel hostname out of cloudflared's output."""
    found = _URL_RE.findall(text or "")
    return found[-1] if found else None


def current_url() -> str | None:
    try:
        url = URL_FILE.read_text().strip()
    except OSError:
        return None
    return url or None


def remember(url: str) -> bool:
    """Store the URL. Returns True if it actually changed — the caller should
    only bother the household when it did."""
    if current_url() == url:
        return False
    URL_FILE.parent.mkdir(parents=True, exist_ok=True)
    URL_FILE.write_text(url + "\n")
    return True


def announce(url: str) -> dict:
    """Email both spouses the new link, and leave it in the shared thread.

    Sent as its own short email rather than folded into a check-in: this is the
    message someone digs up months later on a phone, and it should be findable
    by searching for the word 'portal'.
    """
    from .db import session_scope
    from .messaging import email_thread
    from .models import ChatMessage

    body = (
        f"Your household copilot's portal is reachable from a phone here:\n\n"
        f"{url}\n\n"
        "Sign in with the household password. The link is new because the "
        "tunnel restarted — the old one has stopped working, so replace any "
        "bookmark with this one. I'll email again if it ever changes."
    )
    with session_scope() as session:
        if email_thread.configured():
            email_thread.start_thread(session, "Portal link for your phone", body)
            sent = True
        else:
            session.add(ChatMessage(
                channel="web", role="assistant", speaker="copilot",
                content=f"[portal] new phone link: {url}",
            ))
            sent = False
    return {"url": url, "emailed": sent}


def main() -> None:
    """`python -m bankai.tunnel <cloudflared-log-path>` — called by the tunnel
    service once cloudflared has printed its banner."""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m bankai.tunnel <logfile>")
    text = Path(sys.argv[1]).read_text(errors="replace")
    url = parse_url(text)
    if not url:
        log.warning("tunnel: no URL in %s yet", sys.argv[1])
        return
    if remember(url):
        log.info("tunnel: new URL %s — telling the household", url)
        log.info("tunnel: %s", announce(url))
    else:
        log.info("tunnel: URL unchanged (%s)", url)


if __name__ == "__main__":
    main()
