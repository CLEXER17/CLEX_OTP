"""Offline checks for the response parsers and the store.

No network, no Telegram. Run with:  python test_client.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

from store import CANCELLED, LIVE, Activation, Store
from tempora import parse_v3_country, parse_v3_providers, API_ERRORS, TemporaError, TemporaSMS

PASS, FAIL = 0, 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


class FakeAPI(TemporaSMS):
    """TemporaSMS with the transport swapped for a scripted response list."""

    def __init__(self, responses: list[str]) -> None:
        super().__init__("test-key")
        self._responses = list(responses)
        self.calls: list[str] = []

    async def _call(self, action: str, **params) -> str:
        self.calls.append(action)
        body = self._responses.pop(0)
        head = body.split(":", 1)[0].strip().upper()
        if head in API_ERRORS:
            raise TemporaError(head, body)
        if not body:
            raise TemporaError("EMPTY_RESPONSE", "")
        return body


async def test_balance() -> None:
    print("\nget_balance")
    api = FakeAPI(["ACCESS_BALANCE:100.1234"])
    check("parses value", await api.get_balance(), 100.1234)

    api = FakeAPI(["BAD_KEY"])
    try:
        await api.get_balance()
        check("raises on BAD_KEY", "no raise", "TemporaError")
    except TemporaError as exc:
        check("raises on BAD_KEY", exc.code, "BAD_KEY")

    api = FakeAPI(["something weird"])
    try:
        await api.get_balance()
        check("raises on garbage", "no raise", "TemporaError")
    except TemporaError as exc:
        check("raises on garbage", exc.code, "UNEXPECTED_RESPONSE")
    await api.aclose()


async def test_get_number() -> None:
    print("\nget_number")
    api = FakeAPI(["ACCESS_NUMBER:1234567:919876543210"])
    check("id + phone", await api.get_number("wa", 0), ("1234567", "919876543210"))

    for code in ("NO_NUMBERS", "NO_BALANCE", "BAD_SERVICE"):
        api = FakeAPI([code])
        try:
            await api.get_number("wa", 0)
            check(f"raises on {code}", "no raise", "TemporaError")
        except TemporaError as exc:
            check(f"raises on {code}", exc.code, code)

    api = FakeAPI(["ACCESS_NUMBER:onlyid"])
    try:
        await api.get_number("wa", 0)
        check("raises on short response", "no raise", "TemporaError")
    except TemporaError as exc:
        check("raises on short response", exc.code, "UNEXPECTED_RESPONSE")
    await api.aclose()


async def test_get_status() -> None:
    print("\nget_status")
    cases = [
        ("STATUS_WAIT_CODE", "WAIT_CODE", None),
        ("STATUS_WAIT_RETRY:4821", "WAIT_RETRY", "4821"),
        ("STATUS_OK:903114", "OK", "903114"),
        ("STATUS_CANCEL", "CANCEL", None),
        ("STATUS_SOMETHING_NEW", "UNKNOWN", None),
    ]
    for raw, state, code in cases:
        api = FakeAPI([raw])
        st = await api.get_status("1")
        check(f"{raw} -> state", st.state, state)
        check(f"{raw} -> code", st.code, code)
        await api.aclose()

    api = FakeAPI(["STATUS_OK:903114"])
    st = await api.get_status("1")
    check("has_code true", st.has_code, True)
    await api.aclose()

    # A code that is all zeros must still register as a code.
    api = FakeAPI(["STATUS_OK:000000"])
    st = await api.get_status("1")
    check("zero code still counts", st.has_code, True)
    check("zero code preserved", st.code, "000000")
    await api.aclose()


async def test_setstatus_helpers() -> None:
    print("\nsetStatus helpers")
    api = FakeAPI(["ACCESS_RETRY_GET", "ACCESS_ACTIVATION", "ACCESS_CANCEL"])
    await api.request_retry("1")
    await api.finish("1")
    await api.cancel("1")
    check("three calls made", api.calls, ["setStatus"] * 3)
    await api.aclose()

    api = FakeAPI(["EARLY_CANCEL_DENIED"])
    try:
        await api.cancel("1")
        check("cancel raises when denied", "no raise", "TemporaError")
    except TemporaError as exc:
        check("cancel raises when denied", exc.code, "EARLY_CANCEL_DENIED")
    await api.aclose()


def test_store() -> None:
    print("\nstore")
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    store = Store(path)

    now = time.time()
    act = Activation(
        act_id="a1", phone="919876543210", service="wa", country="0",
        chat_id=42, created_at=now, expires_at=now + 1200, message_id=7,
    )
    store.insert(act)
    check("one live", len(store.live()), 1)

    act.codes.append("111111")
    store.update(act)
    check("code persisted", store.get("a1").codes, ["111111"])

    act.codes.append("222222")
    store.update(act)
    check("second code persisted", store.get("a1").codes, ["111111", "222222"])
    check("stats counts codes", store.stats()["codes_received"], 2)

    act.state = CANCELLED
    store.update(act)
    check("no longer live", len(store.live()), 0)
    check("still in recent", len(store.recent()), 1)

    # expiry maths
    past = Activation(
        act_id="a2", phone="p", service="s", country="0", chat_id=1,
        created_at=now - 3000, expires_at=now - 10,
    )
    check("is_expired true", past.is_expired, True)
    check("seconds_left floors at 0", past.seconds_left, 0)

    future = Activation(
        act_id="a3", phone="p", service="s", country="0", chat_id=1,
        created_at=now, expires_at=now + 65,
    )
    check("is_expired false", future.is_expired, False)
    check("seconds_left sane", 60 <= future.seconds_left <= 65, True)

    store.close()


def test_parse_v3() -> None:
    print("parse_v3_providers")
    doc = {"22": {"wa": {"price": 2.99, "count": 467, "providers": {
        "3": {"count": 467, "price": [2.99], "providerIds": "3"},
        "7": {"count": "12", "price": [3.5, 2.5]},
    }}}}
    check("documented shape", parse_v3_providers(doc, "22", "wa"),
          {"3": (467, [2.99]), "7": (12, [2.5, 3.5])})
    check("service absent -> empty", parse_v3_providers(doc, "22", "tg"), {})
    check("country absent -> None", parse_v3_providers(doc, "1", "wa"), None)
    check("no providers key -> empty",
          parse_v3_providers({"22": {"wa": {}}}, "22", "wa"), {})
    check("not a dict -> None", parse_v3_providers("BAD", "22", "wa"), None)
    check("int country accepted", parse_v3_providers(doc, 22, "wa") is not None, True)
    full = {"22": {"wa": doc["22"]["wa"], "tg": {"count": 0, "providers": {}},
                   "fb": {"providers": {"1": {"count": 5, "price": [1]}}}}}
    check("country-wide", parse_v3_country(full, "22"),
          {"wa": {"3": (467, [2.99]), "7": (12, [2.5, 3.5])}, "fb": {"1": (5, [1.0])}})
    check("country-wide, missing country", parse_v3_country(full, "1"), None)


def test_dedup_logic() -> None:
    """The rule the poller uses to decide a code is new."""
    print("\ndedup")
    codes: list[str] = []
    for incoming in ["111111", "111111", "222222", "222222", "333333"]:
        if incoming not in codes:
            codes.append(incoming)
    check("three distinct kept", codes, ["111111", "222222", "333333"])


async def main() -> None:
    await test_balance()
    await test_get_number()
    await test_get_status()
    await test_setstatus_helpers()
    test_store()
    test_dedup_logic()
    test_parse_v3()
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
