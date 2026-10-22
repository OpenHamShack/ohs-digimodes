"""The two facts about how a Dock360 app is reached, in one place.

This module is small and its whole reason for existing is that both of the
rules below are invisible on a development machine and obvious on a host.

**An app has no fixed address.** The same process is served at
``https://shack/flex/`` on one address, at ``https://flex.example/`` on
another, and at ``/app/flex/`` inside the Dock360 desktop. The reverse proxy
strips whatever it stripped and says so in ``X-Forwarded-Prefix``. So nothing
here may build a URL beginning with a slash: the page is given a ``<base>``
and every link, asset and socket address in it is relative.

**The prefix is a path and nothing else.** A header is whatever the other end
put in it. A value like ``//somewhere-else`` or ``https://somewhere-else``
would turn a redirect after signing in into an open redirect, so a prefix
carrying a scheme, an authority, a backslash or a newline is refused outright
rather than repaired - a repaired one is a value somebody chose and this code
did not understand.
"""

from __future__ import annotations

from aiohttp import web


def prefix(request: web.Request) -> str:
    """The path this app was reached under, or "" - never a trailing slash."""
    value = request.headers.get("X-Forwarded-Prefix", "").strip()
    if not value:
        return ""
    if any(bad in value for bad in ("//", ":", "\\", "\r", "\n", "..")):
        return ""
    if not value.startswith("/"):
        value = "/" + value
    return value.rstrip("/")


def base(request: web.Request) -> str:
    """What goes in ``<base href>``: the prefix, with the trailing slash.

    Always ends in a slash, because a ``<base>`` without one drops its last
    segment and every relative URL on the page then resolves one level too
    high - which works on a subdomain, where the prefix is empty, and breaks
    on a path route, where it is not.
    """
    return prefix(request) + "/"


def cookie_path(request: web.Request) -> str:
    """The path a session cookie should be scoped to.

    Scoping it to the app's own prefix rather than "/" is what stops two
    Dock360 apps on one host overwriting each other's session cookie, which
    presents as being signed out of one app by using another.
    """
    return prefix(request) + "/"


def redirect(request: web.Request, path: str) -> web.HTTPFound:
    """A redirect to somewhere in this app, whatever address it was reached at."""
    return web.HTTPFound(prefix(request) + "/" + path.lstrip("/"))


def wants_json(request: web.Request) -> bool:
    """Whether to answer a refusal with JSON or by sending the browser away.

    A fetch from the page wants a status it can act on; a person typing the
    address wants the sign-in page. Answering either with the other's reply is
    how an expired session turns into a blank screen.
    """
    if request.path.startswith("/api/") or "/api/" in request.path:
        return True
    return "application/json" in request.headers.get("Accept", "")
