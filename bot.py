"""TemporaSMS Telegram bot - single-operator build.

Buys virtual numbers from your own TemporaSMS wallet, polls for OTP codes and
delivers them to your Telegram chat. Locked to one admin id.

Design notes that matter:

* The activation is NEVER auto-finished after the first code. `setStatus 6`
  releases the number for good, so it is only sent when you press Done or the
  window expires. That is what keeps the Retry (another SMS) flow alive.
* One shared poller job handles every live activation, so the per-key rate
  limit is respected no matter how many numbers are open.
* An activation that expires with no code received is auto-cancelled, which is
  what triggers the provider-side refund to your wallet.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import os
import sys
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from store import CANCELLED, DONE, EXPIRED, LIVE, Activation, Store
from tempora import TemporaError, TemporaSMS, parse_v3_country, parse_v3_providers

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
API_KEY = os.environ.get("TEMPORASMS_API_KEY", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
DB_PATH = os.environ.get("DB_PATH", "./bot.db")
DEFAULT_COUNTRY = os.environ.get("DEFAULT_COUNTRY", "22").strip()  # 22 = India
# When locked, commands take no COUNTRY argument and always use DEFAULT_COUNTRY.
LOCK_COUNTRY = os.environ.get("LOCK_COUNTRY", "1").strip().lower() in ("1", "true", "yes")
# Operator for every call: a numeric id from /operators, or a routing mode.
# "smart" is the only mode documented for the list endpoints ("auto" is
# rejected there), so it is the default. Switch at runtime with /op.
OPERATOR_MODES = ("smart", "auto", "cheap", "best")
DEFAULT_OPERATOR = os.environ.get("DEFAULT_OPERATOR", "smart").strip()
current_operator = DEFAULT_OPERATOR
# Observed live 2026-09-21: getServices/getCountries/getPrices reject the
# routing modes despite the docs and need a numeric id. Modes stay valid for
# getNumber. Used by the list commands when current_operator is a mode.
LIST_OPERATOR = os.environ.get("LIST_OPERATOR", "1").strip()
# Catch-all service code for /number (a number not bound to any app). Empty
# means auto-detect from /services by name; /any CODE overrides at runtime.
ANY_SERVICE = os.environ.get("ANY_SERVICE", "").strip()


def list_operator() -> str:
    return current_operator if current_operator.isdigit() else LIST_OPERATOR
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
# Activation window in seconds. 20 min is the protocol convention - confirm
# against your account and adjust if TemporaSMS uses a different window.
ACTIVATION_TTL = int(os.environ.get("ACTIVATION_TTL", "1200"))
MAX_LIVE = int(os.environ.get("MAX_LIVE", "5"))
# Providers refuse cancel for the first minutes of an activation. When a
# cancel is rejected early, retry it automatically once this much time has
# passed since purchase.
CANCEL_AFTER = int(os.environ.get("CANCEL_AFTER", "120"))

_missing = [
    name
    for name, value in (
        ("BOT_TOKEN", BOT_TOKEN),
        ("TEMPORASMS_API_KEY", API_KEY),
        ("ADMIN_ID", ADMIN_ID),
    )
    if not value
]
if _missing:
    sys.exit(f"Missing required environment variables: {', '.join(_missing)}")

store = Store(DB_PATH)
api = TemporaSMS(API_KEY)
buy_lock = asyncio.Lock()

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def admin_only(handler):
    """Reject anyone who is not ADMIN_ID, including on callback queries."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != ADMIN_ID:
            log.warning("Rejected user id=%s", user.id if user else "unknown")
            if update.callback_query:
                await update.callback_query.answer("Not authorised.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("Not authorised.")
            return
        return await handler(update, context)

    wrapper.__name__ = handler.__name__
    return wrapper


def esc(value: object) -> str:
    return html.escape(str(value))


def fmt_left(seconds: int) -> str:
    return f"{seconds // 60}m {seconds % 60:02d}s"


def split_country(args: list[str]) -> tuple[str, list[str]]:
    """(country, remaining args) for a command whose first arg is SERVICE.

    Locked: country is fixed and args[1:] are the rest. Unlocked: args[1] is
    the country when present.
    """
    if LOCK_COUNTRY:
        return DEFAULT_COUNTRY, args[1:]
    if len(args) > 1:
        return args[1], args[2:]
    return DEFAULT_COUNTRY, []


COUNTRY_ARG = "" if LOCK_COUNTRY else " [COUNTRY]"
BUY_USAGE = f"/buy SERVICE{'' if LOCK_COUNTRY else ' COUNTRY'} [MAXPRICE]"


BTN_NUMBER, BTN_ACTIVE = "📱 Get number", "📋 Active"
BTN_STOCK, BTN_BALANCE = "📊 Stock", "💰 Balance"
BTN_CANCEL, BTN_HELP = "🛑 Cancel", "❓ Help"
MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_NUMBER, BTN_ACTIVE], [BTN_STOCK, BTN_BALANCE], [BTN_CANCEL, BTN_HELP]],
    resize_keyboard=True,
    is_persistent=True,
)


def keyboard(act: Activation) -> InlineKeyboardMarkup | None:
    """Action buttons for a live activation."""
    if act.state != LIVE:
        return None
    rows = [
        [
            InlineKeyboardButton("Another SMS", callback_data=f"retry:{act.act_id}"),
            InlineKeyboardButton("Refresh", callback_data=f"look:{act.act_id}"),
        ],
        [
            InlineKeyboardButton("Done (release)", callback_data=f"done:{act.act_id}"),
            InlineKeyboardButton("Cancel + refund", callback_data=f"cncl:{act.act_id}"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def render(act: Activation) -> str:
    """The activation card."""
    label = {
        LIVE: "LIVE",
        DONE: "DONE",
        CANCELLED: "CANCELLED",
        EXPIRED: "EXPIRED",
    }.get(act.state, act.state.upper())

    lines = [
        f"<b>{esc(act.phone)}</b>",
        f"service <code>{esc(act.service)}</code> · country <code>{esc(act.country)}</code>",
        f"id <code>{esc(act.act_id)}</code> · {label}",
    ]
    if act.state == LIVE:
        lines.append(f"window: {fmt_left(act.seconds_left)} left")

    if act.codes:
        lines.append("")
        lines.append("<b>Codes received</b>")
        for i, code in enumerate(act.codes, 1):
            lines.append(f"{i}. <code>{esc(code)}</code>")
    else:
        lines.append("")
        lines.append("<i>waiting for first SMS…</i>")

    if act.note:
        lines.append("")
        lines.append(f"<i>{esc(act.note)}</i>")
    return "\n".join(lines)


async def refresh_card(context: ContextTypes.DEFAULT_TYPE, act: Activation) -> None:
    """Re-render the activation's message in place. Ignores no-op edits."""
    if not act.message_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=act.chat_id,
            message_id=act.message_id,
            text=render(act),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard(act),
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            log.warning("edit failed for %s: %s", act.act_id, exc)
    except RetryAfter as exc:
        # Telegram flood control on rapid edits - skip this tick, resume later.
        log.warning("edit throttled for %s: retry in %ss", act.act_id, exc.retry_after)


async def notify(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

HELP = """<b>TemporaSMS bot</b>

<code>/number</code>  get a number for any app (catch-all service)
<code>{buy_usage}</code>
  number for one app, e.g. <code>/buy wa</code>
<code>/active</code>   live activations
<code>/cancel [ID]</code>  cancel + refund (auto-retries if too early)
<code>/recent</code>   last 15 activations
<code>/balance</code>  wallet balance
<code>/price SERVICE{country_arg}</code>
<code>/stock</code>  every service with stock, per operator
<code>/stock whatsapp</code>  one app, by name or code
<code>/any [CODE]</code>  which service /number buys
<code>/op [N]</code> · <code>/operators</code> · <code>/countries</code> · <code>/services</code>
<code>/stats</code>    local counters

<b>On each activation</b>
· <b>Another SMS</b> — request a second code on the same number.
  Same service only; a different app needs a new number.
· <b>Done</b> — releases the number permanently.
· <b>Cancel</b> — refunds, only before any SMS arrives. If the provider
  says it is too early, the bot waits and cancels for you.

Nothing is auto-released after the first code, so the number stays
yours for the whole window.{lock_note}"""

HELP = HELP.format(
    buy_usage=BUY_USAGE,
    country_arg=COUNTRY_ARG,
    lock_note=f"\n\nCountry is locked to {DEFAULT_COUNTRY}." if LOCK_COUNTRY else "",
)


@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        HELP, parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU
    )


@admin_only
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        bal = await api.get_balance()
    except TemporaError as exc:
        await update.effective_message.reply_text(f"Error: {exc}")
        return
    await update.effective_message.reply_text(
        f"Wallet balance: <b>{bal:.4f}</b>", parse_mode=ParseMode.HTML
    )


@admin_only
async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    args = context.args or []
    if not args:
        await msg.reply_text(f"Usage: {BUY_USAGE}\nExample: /buy wa")
        return

    service = args[0]
    country, rest = split_country(args)
    max_price = None
    if rest:
        try:
            max_price = float(rest[0])
        except ValueError:
            await msg.reply_text("MAXPRICE must be a number.")
            return

    await do_buy(context, msg, service, country, max_price)


def op_label(operator: str) -> str:
    return f"op{operator}" if operator.isdigit() else operator


async def buy_once(
    status_msg, service: str, country: str, operator: str, cap: int | None
) -> tuple[str, str]:
    """One getNumber, auto-filling the price cap for multi-price operators."""
    try:
        return await api.get_number(service, country, operator=operator, max_price=cap)
    except TemporaError as exc:
        if exc.code != "WRONG_MAX_PRICE" or cap is not None:
            raise
        cap = await live_max_price(service, country, operator)
        if cap is None:
            raise
        await status_msg.edit_text(f"Buying via {op_label(operator)}… (multi-price, cap {cap})")
        return await api.get_number(service, country, operator=operator, max_price=cap)


# Rejections that mean "not from this operator" - worth trying the next one.
_TRY_NEXT = ("BAD_SERVICE", "NO_NUMBERS", "BAD_OPERATOR", "WRONG_MAX_PRICE", "ERROR")


async def do_buy(
    context, msg, service: str, country: str, max_price: float | None,
    operator: str | list[str] | None = None,
    plan: list[tuple[str, str]] | None = None,
) -> None:
    """Buy one activation and post its card. Shared by /buy, /number, buttons.

    `operator` may be a list: each is tried in turn until one sells. `plan`
    is the general form - (service, operator) pairs tried in order - and
    overrides `service`/`operator` when given.
    """
    if plan is None:
        candidates = [operator] if isinstance(operator, str) else list(operator or [])
        if not candidates:
            candidates = [current_operator]
        plan = [(service, op) for op in candidates]
    plan = list(dict.fromkeys(plan))  # dedupe, keep order
    services = list(dict.fromkeys(svc for svc, _ in plan))
    async with buy_lock:
        live = store.live()
        if len(live) >= MAX_LIVE:
            await msg.reply_text(
                f"{len(live)} activations already open (limit {MAX_LIVE}). "
                "Finish or cancel one first."
            )
            return

        status_msg = await msg.reply_text("Buying…")
        # The API wants an integer cap; round a fractional one up.
        cap = math.ceil(max_price) if max_price is not None else None
        failures: list[str] = []
        act_id = phone = None
        for service, operator in plan:
            if len(plan) > 1:
                where = f"{service} via {op_label(operator)}" if len(services) > 1 else op_label(operator)
                await status_msg.edit_text(f"Buying {where}…")
            try:
                act_id, phone = await buy_once(status_msg, service, country, operator, cap)
                break
            except TemporaError as exc:
                tag = f"{service} {op_label(operator)}" if len(services) > 1 else op_label(operator)
                failures.append(f"{tag}: {exc.code}")
                if exc.code not in _TRY_NEXT:
                    break
        if act_id is None:
            last = failures[-1].split(": ", 1)[1] if failures else "?"
            hint = ""
            if last == "WRONG_MAX_PRICE":
                hint = f"\nPass a cap: /buy {service} PRICE  (see /stock {service})"
            elif last == "BAD_SERVICE" and len(services) == 1:
                hint = f"\nFind the code: /services {service}"
            detail = "\n".join(failures) if len(failures) > 1 else str(
                TemporaError(last)
            )
            names = await service_names()
            if len(services) == 1:
                label = f"{service} ({names[service]})" if service in names else service
            else:
                label = f"any of {len(services)} catch-all services"
            await status_msg.edit_text(f"Could not buy {label}.\n{detail}{hint}")
            return
        if len(plan) > 1:
            log.info("bought %s on %s after %s", service, op_label(operator), failures or "no failures")

        now = time.time()
        act = Activation(
            act_id=act_id,
            phone=phone,
            service=service,
            country=str(country),
            chat_id=msg.chat_id,
            created_at=now,
            expires_at=now + ACTIVATION_TTL,
            state=LIVE,
            message_id=status_msg.message_id,
        )
        store.insert(act)

    # Tell the provider the number is in hand and the OTP is about to be
    # triggered. Non-fatal if it is rejected - polling still works.
    try:
        await api.set_status(act_id, 1)
    except TemporaError as exc:
        log.info("setStatus(1) on %s returned %s", act_id, exc)

    await refresh_card(context, act)
    log.info("bought %s for service=%s country=%s", phone, service, country)


_any_service: str = ANY_SERVICE
_ANY_PATTERNS = (
    r"^any$", r"^other$", r"^any other", r"^any service", r"^any app",
    r"^other service", r"^all$", r"^all service", r"^universal", r"^full$",
)


async def catch_all_services() -> list[str]:
    """Every catch-all-looking service code, preferred one first.

    /any CODE or ANY_SERVICE pins the first choice; the rest are name matches
    from the merged catalogues, in pattern order.
    """
    names = await service_names()
    lowered = {code: name.lower().strip() for code, name in names.items()}
    found: list[str] = [_any_service] if _any_service else []
    for pattern in _ANY_PATTERNS:
        for code, name in lowered.items():
            if re.search(pattern, name) and code not in found:
                found.append(code)
    return found


def stocked_with_counts(code: str) -> list[tuple[str, int, float | None]]:
    providers = _stock_cache.get("merged", {}).get(code, {})
    return sorted(
        ((pid, c, ps[0] if ps else None) for pid, (c, ps) in providers.items() if c),
        key=lambda t: -t[1],
    )


@admin_only
async def cmd_any(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or pin the catch-all services used by /number."""
    global _any_service
    msg = update.effective_message
    args = context.args or []
    if args:
        _any_service = "" if args[0].lower() in ("auto", "reset", "clear") else args[0]
        await msg.reply_text(
            f"/number tries {_any_service} first." if _any_service
            else "/number back to auto-detected catch-all services."
        )
        return
    codes = await catch_all_services()
    if not codes:
        await msg.reply_text(
            "No catch-all service detected. Find one with /services other "
            "(or any / all) and pin it with /any CODE."
        )
        return
    names = await service_names()
    try:
        await sweep_stock(DEFAULT_COUNTRY, max_age=STOCK_CACHE_TTL)
    except TemporaError:
        pass
    lines = ["<b>/number tries, in order:</b>", ""]
    for code in codes:
        stock = stocked_with_counts(code)
        where = ", ".join(f"op{p} {c}" for p, c, _ in stock[:4]) or "no stock listed"
        lines.append(f"<code>{esc(code)}</code> {esc(names.get(code, ''))} — {esc(where)}")
    lines += ["", "pin one: <code>/any CODE</code> · reset: <code>/any auto</code>"]
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buy a catch-all number - no app asked. Optional PRICE cap.

    Walks every catch-all service, each across the operators that list stock
    for it (then the router), until one sells.
    """
    msg = update.effective_message
    args = context.args or []
    max_price = None
    if args:
        try:
            max_price = float(args[0])
        except ValueError:
            await msg.reply_text("Usage: /number [MAXPRICE]")
            return
    codes = await catch_all_services()
    if not codes:
        await msg.reply_text(
            "No catch-all service found. Run /services other (or any / all), "
            "then /any CODE. Or buy for one app: /buy CODE."
        )
        return
    try:
        await sweep_stock(DEFAULT_COUNTRY, max_age=STOCK_CACHE_TTL)
    except TemporaError as exc:
        log.info("stock lookup for /number failed: %s", exc)

    plan: list[tuple[str, str]] = []
    for code in codes:
        ops = [pid for pid, _, _ in stocked_with_counts(code)]
        plan += [(code, op) for op in ops]
        plan += [(code, m) for m in ("smart", "best")]
    names = await service_names()
    await msg.reply_text(
        "Trying: " + ", ".join(f"{c} ({names.get(c, '?')})" for c in codes)
    )
    await do_buy(context, msg, codes[0], DEFAULT_COUNTRY, max_price, plan=plan)


@admin_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel [ID] - cancel one live activation (the only one, if ID omitted)."""
    msg = update.effective_message
    args = context.args or []
    live = store.live()
    if args:
        act = store.get(args[0])
    elif len(live) == 1:
        act = live[0]
    else:
        await msg.reply_text("Usage: /cancel ID  (ids in /active)")
        return
    if not act or act.state != LIVE:
        await msg.reply_text("No such live activation.")
        return
    result = await try_cancel(context, act)
    replies = {
        "ok": "Cancelled and refunded.",
        "queued": f"Too early — queued, cancels itself {CANCEL_AFTER}s after purchase.",
        "HAS_CODE": "A code already arrived; cancel is refused. Use Done.",
    }
    await msg.reply_text(replies.get(result, f"Cancel rejected: {result}"))


@admin_only
async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persistent keyboard presses arrive as plain text."""
    text = (update.effective_message.text or "").strip()
    context.args = []
    handler = {
        BTN_NUMBER: cmd_number,
        BTN_ACTIVE: cmd_active,
        BTN_STOCK: cmd_stock,
        BTN_BALANCE: cmd_balance,
        BTN_CANCEL: cmd_cancel,
        BTN_HELP: cmd_start,
    }.get(text)
    if handler:
        await handler(update, context)


@admin_only
async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    live = store.live()
    if not live:
        await update.effective_message.reply_text("No live activations.")
        return
    for act in live:
        await update.effective_message.reply_text(
            render(act), parse_mode=ParseMode.HTML, reply_markup=keyboard(act)
        )


@admin_only
async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = store.recent(15)
    if not rows:
        await update.effective_message.reply_text("Nothing yet.")
        return
    lines = ["<b>Recent activations</b>", ""]
    for act in rows:
        codes = ", ".join(act.codes) if act.codes else "—"
        lines.append(
            f"<code>{esc(act.phone)}</code> · {esc(act.service)} · "
            f"{esc(act.state)} · {esc(codes)}"
        )
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


@admin_only
async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(f"Usage: /price SERVICE{COUNTRY_ARG}")
        return
    service = args[0]
    country, _ = split_country(args)
    try:
        data = await api.get_prices(
            service=service, country=country, operator=list_operator()
        )
    except TemporaError as exc:
        await update.effective_message.reply_text(f"Error: {exc}")
        return
    await update.effective_message.reply_text(
        f"<pre>{esc(str(data)[:3500])}</pre>", parse_mode=ParseMode.HTML
    )


@admin_only
async def cmd_op(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global current_operator
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            f"Current operator: {current_operator}\nUsage: /op N  (ids from /operators)"
        )
        return
    choice = args[0].lower()
    if not (choice.isdigit() or choice in OPERATOR_MODES):
        await update.effective_message.reply_text(
            "Operator must be a numeric id or one of: " + ", ".join(OPERATOR_MODES)
        )
        return
    current_operator = choice
    await update.effective_message.reply_text(f"Operator set to {current_operator}.")


@admin_only
async def cmd_operators(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = await api.get_operators()
    except TemporaError as exc:
        await update.effective_message.reply_text(f"Error: {exc}")
        return
    await update.effective_message.reply_text(
        f"Operators (current: {current_operator}):\n"
        f"<pre>{esc(str(data)[:3400])}</pre>",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def cmd_countries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = await api.get_countries(operator=list_operator())
    except TemporaError as exc:
        await update.effective_message.reply_text(f"Error: {exc}")
        return
    await update.effective_message.reply_text(
        f"<pre>{esc(str(data)[:3500])}</pre>", parse_mode=ParseMode.HTML
    )


@admin_only
async def cmd_services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    try:
        data = await api.get_services(operator=list_operator())
    except TemporaError as exc:
        await update.effective_message.reply_text(f"Error: {exc}")
        return
    text = str(data)
    if args:  # crude filter, useful because the list is long
        needle = args[0].lower()
        parts = [p for p in text.replace("{", "").replace("}", "").split(",")
                 if needle in p.lower()]
        text = "\n".join(parts) or "no match"
    await update.effective_message.reply_text(
        f"<pre>{esc(text[:3500])}</pre>", parse_mode=ParseMode.HTML
    )


_services_cache: dict[str, str] = {}


async def live_max_price(service: str, country: str, operator: str) -> int | None:
    """Highest current price for the service on this operator, as an integer cap.

    Multi-price operators refuse getNumber without maxPrice (WRONG_MAX_PRICE);
    the cap is the largest live option so any of them can be reserved.
    """
    providers: dict = {}
    cached = _stock_cache.get("merged", {}).get(service) if _stock_cache.get("country") == country else None
    if cached:
        providers = cached if not operator.isdigit() else {operator: cached.get(operator, (0, []))}
    if not providers or not any(ps for _, ps in providers.values()):
        # Routing modes return nothing useful from getPricesV3; ask a real id.
        lookup_op = operator if operator.isdigit() else list_operator()
        try:
            data = await api.get_prices_v3(country, service=service, operator=lookup_op)
        except TemporaError as exc:
            log.info("price lookup for %s/%s failed: %s", service, lookup_op, exc)
            return None
        providers = parse_v3_providers(data, country, service) or {}
    prices = [p for _, ps in providers.values() for p in ps]
    return math.ceil(max(prices)) if prices else None


async def operator_ids() -> list[str]:
    ops = await api.get_operators()
    values = ops.values() if isinstance(ops, dict) else ops
    return sorted({str(v) for v in values if str(v).isdigit()}, key=int)


async def service_names() -> dict[str, str]:
    """code -> display name, merged across every operator, once per process.

    Each operator carries its own catalogue; a code missing from one list is
    often present in another.
    """
    if not _services_cache:
        try:
            op_ids = await operator_ids()
        except TemporaError as exc:
            log.warning("getOperators failed: %s", exc)
            op_ids = [list_operator()]
        for op in op_ids:
            try:
                data = await api.get_services(operator=op)
            except TemporaError as exc:
                log.warning("getServices(op=%s) failed: %s", op, exc)
                continue
            if isinstance(data, dict):
                for k, v in data.items():
                    _services_cache.setdefault(str(k), str(v))
    return _services_cache


STOCK_CACHE_TTL = 120  # seconds a sweep stays fresh for paging and /number
_stock_cache: dict = {}  # {"ts", "country", "merged", "errors"}


async def sweep_stock(country: str, max_age: float = 0) -> tuple[dict[str, dict], list[str]]:
    """getPricesV3 for every operator, merged: {service: {provider: (count, prices)}}.

    A sweep younger than max_age seconds is reused.
    """
    c = _stock_cache
    if c and c["country"] == country and time.time() - c["ts"] < max_age:
        return c["merged"], c["errors"]
    op_ids = await operator_ids()

    merged: dict[str, dict] = {}
    errors: list[str] = []
    for op in op_ids:
        try:
            data = await api.get_prices_v3(country, operator=op)
        except TemporaError as exc:
            errors.append(f"{op}: {exc.code}")
            if exc.code == "TOO_MANY_REQUESTS":
                break
            continue
        parsed = parse_v3_country(data, country)
        if parsed is None:
            errors.append(f"{op}: unexpected shape {str(data)[:80]!r}")
            continue
        for service, providers in parsed.items():
            slot = merged.setdefault(service, {})
            for pid, (count, prices) in providers.items():
                if pid not in slot or count > slot[pid][0]:
                    slot[pid] = (count, prices)
    _stock_cache.update(ts=time.time(), country=country, merged=merged, errors=errors)
    return merged, errors


def stocked_operators(code: str) -> list[str]:
    """Operator ids listing stock for `code` in the cached sweep, most first."""
    providers = _stock_cache.get("merged", {}).get(code, {})
    return [pid for pid, (c, _) in sorted(providers.items(), key=lambda kv: -kv[1][0]) if c]


def best_operator(providers: dict) -> tuple[str, int, float | None] | None:
    """(operator, count, lowest price) with the most stock, or None."""
    live = [(pid, c, ps[0] if ps else None) for pid, (c, ps) in providers.items() if c]
    if not live:
        return None
    return max(live, key=lambda t: t[1])


def fmt_providers(providers: dict) -> str:
    parts = []
    for pid, (count, prices) in sorted(providers.items(), key=lambda kv: -kv[1][0]):
        if not count:
            continue
        price = f"@{prices[0]:g}" if prices else ""
        parts.append(f"op{pid} {count}{price}")
    return ", ".join(parts) or "—"


def alpha_key(name: str) -> str:
    """Sort key that files '88pika' under P and '7Eleven' under E."""
    stripped = re.sub(r"^[^A-Za-z\u0080-\uffff]+", "", name.strip()).lower()
    return stripped or name.lower()


STOCK_PAGE = 15
_stock_view: dict = {}  # last /stock result for paging: {"title", "ranked", "single"}


def render_stock_page(page: int, names: dict[str, str]) -> tuple[str, InlineKeyboardMarkup | None]:
    """Text + buttons for one page of the cached /stock view."""
    view = _stock_view
    ranked = view["ranked"]
    pages = max(1, math.ceil(len(ranked) / STOCK_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = ranked[page * STOCK_PAGE:(page + 1) * STOCK_PAGE]

    lines = [f"<b>India · {view['title']}</b>  (page {page + 1}/{pages}, {len(ranked)} services)", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for code, providers in chunk:
        total = sum(c for c, _ in providers.values())
        name = names.get(code, "")
        label = f"<code>{esc(code)}</code> {esc(name)}".strip()
        lines.append(f"{label}: <b>{total}</b>  ({esc(fmt_providers(providers))})")

    if view["single"] and chunk:
        code, providers = chunk[0]
        for pid, (count, prices) in sorted(providers.items(), key=lambda kv: -kv[1][0]):
            if not count:
                continue
            price = f" @{prices[0]:g}" if prices else ""
            row.append(InlineKeyboardButton(f"op{pid} · {count}{price}", callback_data=f"buy:{code}:{pid}"))
            if len(row) == 2:
                buttons.append(row); row = []
    else:
        for code, providers in chunk:
            if not sum(c for c, _ in providers.values()):
                continue
            name = names.get(code)
            label = f"{name[:12]} ({code})" if name else code
            row.append(InlineKeyboardButton(label, callback_data=f"buy:{code}:"))
            if len(row) == 2:
                buttons.append(row); row = []
    if row:
        buttons.append(row)

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"pg:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="pg:noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"pg:{page + 1}"))
        buttons.append(nav)

    if not ranked:
        lines.append("nothing in stock")
    if view.get("errors"):
        lines += ["", "<i>" + esc("; ".join(view["errors"])) + "</i>"]
    lines += ["", "tap Buy, or <code>/stock NAME</code> to narrow"]
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(buttons) if buttons else None


@admin_only
async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Live stock per operator for India, paged.

    /stock            every service with stock, biggest first
    /stock wa         one service by code
    /stock whatsapp   services whose name contains the text
    """
    msg = update.effective_message
    args = context.args or []
    country, _ = split_country(args) if args else (DEFAULT_COUNTRY, [])
    query = args[0].lower() if args else ""

    status = await msg.reply_text("Sweeping operators…")
    try:
        merged, errors = await sweep_stock(country, max_age=STOCK_CACHE_TTL)
    except TemporaError as exc:
        await status.edit_text(f"Error: {exc}")
        return
    names = await service_names()

    if query:
        if query in merged or query in names:
            picked = {query: merged.get(query, {})}
        else:
            picked = {c: p for c, p in merged.items() if query in names.get(c, "").lower()}
            if not picked:
                hits = [c for c, n in names.items() if query in n.lower()]
                if hits:
                    await status.edit_text(
                        "No stock right now for: " +
                        ", ".join(f"{c} ({names[c]})" for c in hits[:10])
                    )
                else:
                    await status.edit_text(f"No service matches '{query}'. Try /services {query}")
                return
        title = f"stock for '{esc(query)}'"
    else:
        picked = {c: p for c, p in merged.items() if sum(x for x, _ in p.values())}
        title = "all services with stock"

    # Alphabetical by display name (code when unnamed), so paging is browsable.
    ranked = sorted(picked.items(), key=lambda kv: (alpha_key(names.get(kv[0], kv[0])), kv[0]))
    _stock_view.clear()
    _stock_view.update(title=title, ranked=ranked, single=len(ranked) == 1, errors=errors)
    text, markup = render_stock_page(0, names)
    await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store.stats()
    lines = ["<b>Local counters</b>", ""]
    for key, value in sorted(s.items()):
        lines.append(f"{esc(key)}: <b>{value}</b>")
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


# --------------------------------------------------------------------------
# cancel
# --------------------------------------------------------------------------

_EARLY_CANCEL = ("EARLY_CANCEL_DENIED", "BAD_STATUS", "ERROR")


CANCEL_RETRY_EVERY = 30   # seconds between retries once CANCEL_AFTER has passed
CANCEL_RETRIES = 6        # give up after this many post-window retries


async def try_cancel(
    context: ContextTypes.DEFAULT_TYPE, act: Activation, attempt: int = 0
) -> str:
    """Cancel + refund. Returns 'ok', 'queued', or the rejection code.

    Always tries immediately. If the provider rejects it as too early, a
    one-shot job retries when CANCEL_AFTER has passed, then every
    CANCEL_RETRY_EVERY seconds up to CANCEL_RETRIES times.
    """
    if act.codes:
        return "HAS_CODE"
    try:
        await api.cancel(act.act_id)
    except TemporaError as exc:
        elapsed = time.time() - act.created_at
        wait = CANCEL_AFTER - elapsed
        if exc.code in _EARLY_CANCEL and (wait > 0 or attempt < CANCEL_RETRIES):
            delay = wait + 2 if wait > 0 else CANCEL_RETRY_EVERY
            act.note = f"cancel queued — retrying in {int(delay)}s ({exc.code})"
            store.update(act)
            await refresh_card(context, act)
            context.job_queue.run_once(
                cancel_job, when=delay,
                data={"id": act.act_id, "attempt": attempt + (wait <= 0)},
                name=f"cancel:{act.act_id}",
            )
            return "queued"
        act.note = f"cancel rejected: {exc}"
        store.update(act)
        await refresh_card(context, act)
        return exc.code
    act.state = CANCELLED
    act.note = "cancelled, refund requested"
    store.update(act)
    await refresh_card(context, act)
    return "ok"


async def cancel_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    act = store.get(data["id"])
    if not act or act.state != LIVE:
        return
    if act.codes:
        act.note = "cancel dropped — a code arrived meanwhile"
        store.update(act)
        await refresh_card(context, act)
        return
    result = await try_cancel(context, act, attempt=data["attempt"])
    if result == "queued":
        return
    text = ("cancelled and refunded" if result == "ok"
            else f"cancel failed: {result}")
    await notify(
        context, act.chat_id,
        f"<code>{esc(act.act_id)}</code> ({esc(act.phone)}): {esc(text)}",
    )


# --------------------------------------------------------------------------
# buttons
# --------------------------------------------------------------------------


@admin_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, act_id = (query.data or "").partition(":")

    if action == "pg":
        if act_id == "noop":
            await query.answer()
            return
        if not _stock_view:
            await query.answer("Stock view expired — run /stock again.", show_alert=True)
            return
        text, markup = render_stock_page(int(act_id), await service_names())
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        await query.answer()
        return

    if action == "buy":
        code, _, op = act_id.partition(":")
        if op:
            candidates: list[str] = [op]
        else:
            candidates = stocked_operators(code)
            candidates += [m for m in (current_operator, "smart") if m not in candidates]
        await query.answer(f"Buying {code}" + (f" via op{op}" if op else ""))
        await do_buy(context, query.message, code, DEFAULT_COUNTRY, None, candidates)
        return

    act = store.get(act_id)
    if not act:
        await query.answer("Unknown activation.", show_alert=True)
        return

    if action == "look":
        await query.answer("Refreshed")
        await refresh_card(context, act)
        return

    if action == "retry":
        if act.state != LIVE:
            await query.answer("Activation is closed.", show_alert=True)
            return
        try:
            await api.request_retry(act.act_id)
        except TemporaError as exc:
            await query.answer(f"Retry rejected: {exc.code}", show_alert=True)
            act.note = f"retry rejected: {exc}"
            store.update(act)
            await refresh_card(context, act)
            return
        await query.answer("Asked for another SMS — trigger it now.")
        act.note = "waiting for another SMS (same service)"
        store.update(act)
        await refresh_card(context, act)
        return

    if action == "done":
        await query.answer()
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[
                InlineKeyboardButton("Yes, release it", callback_data=f"dyes:{act_id}"),
                InlineKeyboardButton("Keep it", callback_data=f"look:{act_id}"),
            ]])
        )
        return

    if action == "dyes":
        try:
            await api.finish(act.act_id)
        except TemporaError as exc:
            log.info("finish(%s) -> %s", act.act_id, exc)
        act.state = DONE
        act.note = "released"
        store.update(act)
        await query.answer("Released.")
        await refresh_card(context, act)
        return

    if action == "cncl":
        if act.codes:
            await query.answer(
                "A code already arrived — cancel is refused after the first SMS. "
                "Use Done instead.",
                show_alert=True,
            )
            return
        result = await try_cancel(context, act)
        if result == "ok":
            await query.answer("Cancelled and refunded.")
        elif result == "queued":
            await query.answer(
                f"Too early — provider allows cancel {CANCEL_AFTER}s after purchase. "
                "Queued; it will cancel itself.",
                show_alert=True,
            )
        else:
            await query.answer(f"Cancel rejected: {result}", show_alert=True)
        return

    await query.answer("Unknown action.")


# --------------------------------------------------------------------------
# poller
# --------------------------------------------------------------------------


async def poll_activations(context: ContextTypes.DEFAULT_TYPE) -> None:
    """One job for every live activation. Runs every POLL_INTERVAL seconds."""
    for act in store.live():

        # ---- expiry first, so we stop paying attention to dead windows ----
        if act.is_expired:
            if act.codes:
                try:
                    await api.finish(act.act_id)
                except TemporaError:
                    pass
                act.state = EXPIRED
                act.note = "window expired"
            else:
                try:
                    await api.cancel(act.act_id)
                    act.note = "expired with no SMS — cancelled for refund"
                except TemporaError as exc:
                    act.note = f"expired; cancel returned {exc.code}"
                act.state = CANCELLED
            store.update(act)
            await refresh_card(context, act)
            await notify(
                context, act.chat_id,
                f"Activation <code>{esc(act.act_id)}</code> "
                f"({esc(act.phone)}) closed: {esc(act.note)}",
            )
            continue

        # ---- poll ----
        try:
            status = await api.get_status(act.act_id)
        except TemporaError as exc:
            if exc.code in ("NO_ACTIVATION", "WRONG_ACTIVATION_ID"):
                act.state = EXPIRED
                act.note = "provider closed this activation"
                store.update(act)
                await refresh_card(context, act)
            elif exc.code == "TOO_MANY_REQUESTS":
                log.warning("rate limited; backing off this cycle")
                return
            else:
                log.warning("poll %s failed: %s", act.act_id, exc)
            continue

        if status.state == "CANCEL":
            act.state = CANCELLED
            act.note = "cancelled at the provider"
            store.update(act)
            await refresh_card(context, act)
            continue

        if status.has_code and status.code not in act.codes:
            act.codes.append(status.code)
            act.note = None
            store.update(act)
            await refresh_card(context, act)
            index = len(act.codes)
            await notify(
                context, act.chat_id,
                f"<b>Code {index}</b> for {esc(act.phone)}: "
                f"<code>{esc(status.code)}</code>",
            )
            continue

        # Nothing changed - still redraw so the countdown ticks every cycle.
        await refresh_card(context, act)


async def on_startup(app: Application) -> None:
    live = store.live()
    log.info("Rehydrated %d live activation(s) from %s", len(live), DB_PATH)
    if live and ADMIN_ID:
        await app.bot.send_message(
            ADMIN_ID,
            f"Bot restarted. {len(live)} activation(s) still open — /active",
        )


async def on_shutdown(app: Application) -> None:
    await api.aclose()
    store.close()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        # Another instance is polling - normally the previous Railway deploy
        # still draining. PTB keeps retrying; this clears once it exits.
        log.warning("Another bot instance is polling (old deploy draining?); retrying")
        return
    log.exception("Unhandled error", exc_info=context.error)


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler(["number", "num", "get"], cmd_number))
    app.add_handler(CommandHandler("any", cmd_any))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("active", cmd_active))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("op", cmd_op))
    app.add_handler(CommandHandler("operators", cmd_operators))
    app.add_handler(CommandHandler("countries", cmd_countries))
    app.add_handler(CommandHandler("services", cmd_services))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(
        poll_activations, interval=POLL_INTERVAL, first=3, name="poller"
    )

    log.info("Starting bot (admin=%s, poll=%ss, ttl=%ss)",
             ADMIN_ID, POLL_INTERVAL, ACTIVATION_TTL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
