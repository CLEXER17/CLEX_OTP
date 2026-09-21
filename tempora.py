"""Async client for the TemporaSMS API (handler_api.php).

All requests go to a single endpoint with an `action` parameter. Most v1
actions return a bare text string like `ACCESS_BALANCE:100.1234` rather than
JSON, so every response is parsed defensively: anything that isn't a known
success prefix is raised as a TemporaError carrying the raw body.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.temporasms.com/stubs/handler_api.php"

# Error strings the API returns in place of a result. The first block is
# documented on temporasms.com; the rest are standard to this API family and
# are handled so they surface as readable messages instead of parse errors.
API_ERRORS = {
    "BAD_ACTION": "Invalid action requested.",
    "BAD_KEY": "Invalid or missing API key.",
    "USER_BANNED": "This account has been banned.",
    "ERROR": "Generic server error.",
    "TOO_MANY_REQUESTS": "Rate limit exceeded - slow down.",
    "UNDER_DEVELOPMENT": "This feature is under development.",
    # Common to the handler_api.php protocol family:
    "BAD_SERVICE": "Unknown service code.",
    "BAD_STATUS": "Invalid status value.",
    "NO_NUMBERS": "No numbers available for that service/country right now.",
    "NO_BALANCE": "Insufficient balance on the TemporaSMS wallet.",
    "NO_ACTIVATION": "Unknown or already-closed activation id.",
    "EARLY_CANCEL_DENIED": "Too soon after purchase to cancel.",
    "WRONG_ACTIVATION_ID": "Unknown activation id.",
    # Observed live (2026-09-21): list endpoints return this without ?operator=
    "BAD_OPERATOR": "Unknown or missing operator - see /operators.",
}

# setStatus values.
STATUS_READY = 1      # number is ready / you have triggered the OTP
STATUS_RETRY = 3      # request another SMS on this same activation
STATUS_FINISH = 6     # complete the activation (IRREVERSIBLE - releases number)
STATUS_CANCEL = 8     # cancel and refund (only before any SMS arrives)


class TemporaError(RuntimeError):
    """An API-level error response."""

    def __init__(self, code: str, raw: str = "") -> None:
        self.code = code
        self.raw = raw
        detail = API_ERRORS.get(code, "")
        msg = f"{code} - {detail}" if detail else code
        # Transport/parse failures carry no documented meaning; surface the body.
        if raw and code in ("BAD_JSON", "HTTP_ERROR", "NETWORK_ERROR", "EMPTY_RESPONSE"):
            msg = f"{msg}: {raw[:300]}"
        super().__init__(msg)


@dataclass(frozen=True)
class SmsStatus:
    """Parsed result of a getStatus call."""

    state: str                # WAIT_CODE | WAIT_RETRY | OK | CANCEL | UNKNOWN
    code: str | None = None   # the OTP, when present
    raw: str = ""

    @property
    def has_code(self) -> bool:
        return self.state == "OK" and bool(self.code)

    @property
    def is_terminal(self) -> bool:
        return self.state == "CANCEL"


class RateLimiter:
    """Serialises calls and enforces a minimum gap between them.

    The API rate-limits per key, so every request in the process goes through
    one limiter rather than per-activation pollers racing each other.
    """

    def __init__(self, min_interval: float = 0.5) -> None:
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._min_interval:
                await asyncio.sleep(self._min_interval - gap)
            self._last = time.monotonic()


class TemporaSMS:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
        min_interval: float = 0.5,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._key = api_key
        self._base_url = base_url
        self._limiter = RateLimiter(min_interval)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "temporasms-bot/1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    async def _call(self, action: str, **params: Any) -> str:
        """Perform one API call and return the raw body, raising on errors."""
        query = {"api_key": self._key, "action": action}
        for key, value in params.items():
            if value is not None:
                query[key] = str(value)

        await self._limiter.wait()
        try:
            resp = await self._client.get(self._base_url, params=query)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TemporaError("HTTP_ERROR", f"HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise TemporaError("NETWORK_ERROR", str(exc)) from exc

        body = resp.text.strip()
        # Never log the key; log the action and a truncated body only.
        log.debug("action=%s -> %s", action, body[:200])

        head = body.split(":", 1)[0].strip().upper()
        if head in API_ERRORS:
            raise TemporaError(head, body)
        if not body:
            raise TemporaError("EMPTY_RESPONSE", "")
        return body

    @staticmethod
    def _as_json(body: str) -> Any:
        try:
            return json.loads(body.lstrip("﻿"))
        except json.JSONDecodeError as exc:
            log.warning("non-JSON body (%d bytes): %r", len(body), body[:400])
            raise TemporaError("BAD_JSON", body[:400]) from exc

    # ------------------------------------------------------------------
    # endpoints
    # ------------------------------------------------------------------

    async def get_balance(self) -> float:
        """ACCESS_BALANCE:100.1234 -> 100.1234"""
        body = await self._call("getBalance")
        if not body.upper().startswith("ACCESS_BALANCE"):
            raise TemporaError("UNEXPECTED_RESPONSE", body)
        try:
            return float(body.split(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise TemporaError("UNEXPECTED_RESPONSE", body) from exc

    async def get_number(
        self,
        service: str,
        country: str | int,
        *,
        operator: str | None = None,
        max_price: float | None = None,
    ) -> tuple[str, str]:
        """Buy an activation. Returns (activation_id, phone_number).

        ACCESS_NUMBER:<id>:<phone>

        `operator` accepts a numeric id or one of smart / cheap / auto / best.
        Omitting it with `max_price` set selects price-first auto mode.
        """
        body = await self._call(
            "getNumber",
            service=service,
            country=country,
            operator=operator,
            maxPrice=max_price,
        )
        parts = body.split(":")
        if parts[0].upper() != "ACCESS_NUMBER" or len(parts) < 3:
            raise TemporaError("UNEXPECTED_RESPONSE", body)
        return parts[1].strip(), parts[2].strip()

    async def get_status(self, activation_id: str) -> SmsStatus:
        """Poll one activation for its SMS.

        STATUS_WAIT_CODE         waiting for the first SMS
        STATUS_WAIT_RETRY:<last> a code arrived; another was requested
        STATUS_OK:<code>         code received
        STATUS_CANCEL            activation cancelled
        """
        body = await self._call("getStatus", id=activation_id)
        upper = body.upper()

        if upper.startswith("STATUS_OK"):
            code = body.split(":", 1)[1].strip() if ":" in body else None
            return SmsStatus("OK", code, body)
        if upper.startswith("STATUS_WAIT_RETRY"):
            last = body.split(":", 1)[1].strip() if ":" in body else None
            return SmsStatus("WAIT_RETRY", last, body)
        if upper.startswith("STATUS_WAIT_CODE") or upper.startswith("STATUS_WAIT"):
            return SmsStatus("WAIT_CODE", None, body)
        if upper.startswith("STATUS_CANCEL"):
            return SmsStatus("CANCEL", None, body)
        return SmsStatus("UNKNOWN", None, body)

    async def set_status(self, activation_id: str, status: int) -> str:
        """Change an activation's status. See the STATUS_* constants."""
        return await self._call("setStatus", id=activation_id, status=status)

    async def request_retry(self, activation_id: str) -> str:
        """Ask for another SMS on the same number (same service only)."""
        return await self.set_status(activation_id, STATUS_RETRY)

    async def finish(self, activation_id: str) -> str:
        """Complete the activation. Irreversible - the number is released."""
        return await self.set_status(activation_id, STATUS_FINISH)

    async def cancel(self, activation_id: str) -> str:
        """Cancel and refund. Only works before any SMS has arrived."""
        return await self.set_status(activation_id, STATUS_CANCEL)

    async def get_prices(
        self,
        service: str | None = None,
        country: str | int | None = None,
        operator: str | None = None,
    ) -> Any:
        body = await self._call(
            "getPrices", service=service, country=country, operator=operator
        )
        return self._as_json(body)

    async def get_countries(self, operator: str | None = None) -> Any:
        return self._as_json(await self._call("getCountries", operator=operator))

    async def get_services(self, operator: str | None = None) -> Any:
        return self._as_json(await self._call("getServices", operator=operator))

    async def get_operators(self, country: str | int | None = None) -> Any:
        return self._as_json(await self._call("getOperators", country=country))
