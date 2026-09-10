"""HTTP client for a Stage Utility server.

Stage Utility is a LAN appliance served over plain HTTP with no login. Only the
cue call route carries a credential: a bearer token minted in the app's settings.
Everything this integration reads — the manifest, the states snapshot, the event
stream — is an open read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import DEFAULT_PORT, DEFAULT_SCHEME, LOGGER, PROBE_CUE

REQUEST_TIMEOUT = ClientTimeout(total=10)


class StageUtilityError(Exception):
    """Base error for anything this client could not complete."""


class CannotConnect(StageUtilityError):
    """The server could not be reached, or answered something unusable."""


class TooOld(StageUtilityError):
    """The server answered but has no cue manifest: Stage Utility before 1.18.0."""


class InvalidAuth(StageUtilityError):
    """The server refused the bearer token."""


class CueUnknown(StageUtilityError):
    """The server has no cue by that name."""


class CueRefused(StageUtilityError):
    """The server refused to run the cue, and said why in a sentence.

    The message is the server's own `error` field. It is written for a person —
    "The Gospel Way is live" — so it is passed through to the operator verbatim
    rather than being translated into an integration-flavoured one.
    """

    def __init__(self, message: str, reason: str, plan: str | None = None) -> None:
        """Keep the machine-readable reason beside the sentence."""
        super().__init__(message)
        self.reason = reason
        self.plan = plan


@dataclass(slots=True)
class CueResult:
    """What a successful cue call reported."""

    ok: bool
    detail: str
    state: str | None = None
    skipped: bool = False
    #: The server's automation engine is in simulate mode, so it reported what
    #: it *would* have pressed and pressed nothing. A fresh Stage Utility
    #: install defaults to simulate, so this is the state a new integration is
    #: most likely to meet first.
    simulated: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def normalise_host(raw: str) -> str:
    """Turn what an operator typed into `scheme://host:port`.

    Accepts a bare IP, `host:port`, or a full URL. Bare hosts get plain HTTP on
    the default port, which is how every Stage Utility install starts out.
    """
    value = raw.strip().rstrip("/")
    if not value:
        raise CannotConnect("No host given")
    if "://" not in value:
        value = f"{DEFAULT_SCHEME}://{value}"
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise CannotConnect(f"Unsupported scheme {parts.scheme!r}")
    if not parts.hostname:
        raise CannotConnect(f"{raw!r} has no host in it")
    port = parts.port
    if port is None:
        port = DEFAULT_PORT if parts.scheme == "http" else 443
    host = parts.hostname
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"{parts.scheme}://{host}:{port}"


def host_of(url: str) -> str:
    """The bare hostname of a URL, for use as the config entry's unique id."""
    return urlsplit(url).hostname or url


class StageUtilityApi:
    """Talks to one Stage Utility server."""

    def __init__(self, session: ClientSession, base_url: str, token: str) -> None:
        """Hold the shared aiohttp session; this client never opens its own."""
        self.session = session
        self.base_url = base_url
        self._token = token

    @property
    def token(self) -> str:
        """The bearer token, for the SSE task and diagnostics redaction."""
        return self._token

    def url(self, path: str) -> str:
        """Absolute URL for a server path."""
        return f"{self.base_url}{path}"

    async def _get_json(self, path: str) -> dict[str, Any]:
        try:
            async with self.session.get(self.url(path), timeout=REQUEST_TIMEOUT) as response:
                if response.status != 200:
                    raise CannotConnect(f"{path} answered HTTP {response.status}")
                body = await response.json(content_type=None)
        except ClientError as err:
            raise CannotConnect(f"{path} could not be reached: {err}") from err
        except TimeoutError as err:
            raise CannotConnect(f"{path} timed out") from err
        if not isinstance(body, dict):
            raise CannotConnect(f"{path} did not answer an object")
        return body

    async def async_get_manifest(self) -> dict[str, Any]:
        """The cue manifest: the switches and buttons this server offers."""
        # A 404 here is a Stage Utility that IS running and reachable but predates
        # the manifest (1.18.0). Told apart from a dead address because the fix
        # is different: update the server, not the network. A first install was
        # pointed at a 1.17.1 production box and read "did not answer".
        try:
            return await self._get_json("/api/cues/manifest")
        except CannotConnect as err:
            if "HTTP 404" in str(err):
                raise TooOld("this Stage Utility has no cue manifest; 1.18.0 or newer is needed") from err
            raise

    async def async_get_states(self) -> dict[str, Any]:
        """A states snapshot. Only polled while the event stream is down."""
        return await self._get_json("/api/cues/states")

    async def async_verify_token(self) -> None:
        """Prove the token is accepted, without running anything.

        The call route checks the bearer token before it looks the cue up, so a
        cue name that cannot exist separates "bad token" (401) from "token fine"
        (404) without pressing a button on real gear.
        """
        status, _ = await self._post_cue(PROBE_CUE, None)
        if status == 401:
            raise InvalidAuth("The server refused this token")
        if status == 404:
            return
        raise CannotConnect(f"Token probe answered an unexpected HTTP {status}")

    async def _post_cue(self, name: str, confirm: str | None) -> tuple[int, dict[str, Any]]:
        path = f"/api/cues/{name}"
        params = {"confirm": confirm} if confirm else None
        try:
            async with self.session.post(
                self.url(path),
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=REQUEST_TIMEOUT,
            ) as response:
                body = await response.json(content_type=None)
                return response.status, body if isinstance(body, dict) else {}
        except ClientError as err:
            raise CannotConnect(f"Cue {name} could not be called: {err}") from err
        except TimeoutError as err:
            raise CannotConnect(f"Cue {name} timed out") from err

    async def async_call_cue(self, name: str) -> CueResult:
        """Run a cue, answering a two-step confirmation on the operator's behalf.

        A `202` means the server wants the call repeated with a token. Home
        Assistant has no way to ask a second time — the operator already pressed
        the switch, or an automation already decided — so the confirmation is
        sent straight back. This is documented in the README.
        """
        status, body = await self._post_cue(name, None)
        if status == 202 and isinstance(body.get("confirm"), str):
            LOGGER.debug("Cue %s asked for confirmation; confirming", name)
            status, body = await self._post_cue(name, body["confirm"])
            if status == 202:
                raise CannotConnect(f"Cue {name} asked to be confirmed twice; giving up")
        return self._result(name, status, body)

    @staticmethod
    def _result(name: str, status: int, body: dict[str, Any]) -> CueResult:
        if status == 200:
            return CueResult(
                ok=bool(body.get("ok", True)),
                detail=str(body.get("detail", "")),
                state=body.get("state"),
                skipped=bool(body.get("skipped", False)),
                simulated=bool(body.get("simulated", False)),
                raw=body,
            )
        message = str(body.get("error") or f"HTTP {status}")
        if status == 401:
            raise InvalidAuth(message)
        if status == 404:
            raise CueUnknown(f"{message} (cue {name})")
        if status == 409:
            raise CueRefused(message, str(body.get("reason", "")), body.get("plan"))
        raise CannotConnect(f"Cue {name} answered HTTP {status}: {message}")
