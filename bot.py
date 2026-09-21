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
from telegram.error import BadRequest
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


async def do_buy(
    context, msg, service: str, country: str, max_price: float | None,
    operator: str | None = None,
) -> None:
    """Buy one activation and post its card. Shared by /buy, /number, buttons."""
    operator = operator or current_operator
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
        try:
            try:
                act_id, phone = await api.get_number(
                    service, country, operator=operator, max_price=cap
                )
            except TemporaError as exc:
                if exc.code != "WRONG_MAX_PRICE" or cap is not None:
                    raise
                cap = await live_max_price(service, country, operator)
                if cap is None:
                    raise
                await status_msg.edit_text(f"Buying… (multi-price operator, cap {cap})")
                act_id, phone = await api.get_number(
                    service, country, operator=operator, max_price=cap
                )
        except TemporaError as exc:
            hint = ""
            if exc.code == "WRONG_MAX_PRICE":
                hint = f"\nPass a cap: /buy {service} PRICE  (see /stock {service})"
            elif exc.code == "BAD_SERVICE":
                hint = f"\nFind the code: /services {service}"
            await status_msg.edit_text(f"Could not buy a number.\n{exc}{hint}")
            return

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
    r"^other service", r"^all$", r"^all service", r"^universal",
)


async def resolve_any_service() -> str | None:
    """Catch-all service code: env/override, else first name match in /services."""
    global _any_service
    if _any_service:
        return _any_service
    names = await service_names()
    lowered = {code: name.lower().strip() for code, name in names.items()}
    for pattern in _ANY_PATTERNS:
        for code, name in lowered.items():
            if re.search(pattern, name):
                _any_service = code
                log.info("catch-all service auto-detected: %s (%s)", code, names[code])
                return code
    return None


@admin_only
async def cmd_any(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or set the catch-all service used by /number."""
    global _any_service
    args = context.args or []
    if args:
        _any_service = args[0]
        await update.effective_message.reply_text(f"/number will buy service {_any_service}.")
        return
    code = await resolve_any_service()
    names = await service_names()
    if code:
        await update.effective_message.reply_text(
            f"/number buys {code} ({names.get(code, '?')}). Change with /any CODE."
        )
    else:
        await update.effective_message.reply_text(
            "No catch-all service detected. Find it with /services other "
            "(or any / all) and set it with /any CODE."
        )


@admin_only
async def cmd_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buy a catch-all number - no app asked. Optional PRICE cap."""
    msg = update.effective_message
    args = context.args or []
    max_price = None
    if args:
        try:
            max_price = float(args[0])
        except ValueError:
            await msg.reply_text("Usage: /number [MAXPRICE]")
            return
    code = await resolve_any_service()
    if not code:
        await msg.reply_text(
            "No catch-all service set. Run /services other (or any / all), "
            "then /any CODE. Or buy for one app: /buy CODE."
        )
        return
    await do_buy(context, msg, code, DEFAULT_COUNTRY, max_price)


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
    try:
        data = await api.get_prices_v3(country, service=service, operator=operator)
    except TemporaError as exc:
        log.info("price lookup for %s/%s failed: %s", service, operator, exc)
        return None
    providers = parse_v3_providers(data, country, service) or {}
    prices = [p for _, ps in providers.values() for p in ps]
    return math.ceil(max(prices)) if prices else None


async def service_names() -> dict[str, str]:
    """code -> display name, fetched once per process."""
    if not _services_cache:
        try:
            data = await api.get_services(operator=list_operator())
            if isinstance(data, dict):
                _services_cache.update({str(k): str(v) for k, v in data.items()})
        except TemporaError as exc:
            log.warning("getServices failed: %s", exc)
    return _services_cache


async def sweep_stock(country: str) -> tuple[dict[str, dict], list[str]]:
    """getPricesV3 for every operator, merged: {service: {provider: (count, prices)}}."""
    ops = await api.get_operators()
    values = ops.values() if isinstance(ops, dict) else ops
    op_ids = sorted({str(v) for v in values if str(v).isdigit()}, key=int)

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
    return merged, errors


def fmt_providers(providers: dict) -> str:
    parts = []
    for pid, (count, prices) in sorted(providers.items(), key=lambda kv: -kv[1][0]):
        if not count:
            continue
        price = f"@{prices[0]:g}" if prices else ""
        parts.append(f"op{pid} {count}{price}")
    return ", ".join(parts) or "—"


@admin_only
async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Live stock per operator for India.

    /stock            every service with stock, biggest first
    /stock wa         one service by code
    /stock whatsapp   services whose name contains the text
    """
    msg = update.effective_message
    args = context.args or []
    country, rest = split_country(args) if args else (DEFAULT_COUNTRY, [])
    query = args[0].lower() if args else ""

    status = await msg.reply_text("Sweeping operators…")
    try:
        merged, errors = await sweep_stock(country)
    except TemporaError as exc:
        await status.edit_text(f"Error: {exc}")
        return
    names = await service_names()

    if query:
        if query in merged or query in names:
            picked = {query: merged.get(query, {})}
        else:
            picked = {
                code: prov for code, prov in merged.items()
                if query in names.get(code, "").lower()
            }
            if not picked:
                hits = [c for c, n in names.items() if query in n.lower()]
                if hits:
                    await status.edit_text(
                        f"No stock right now for: " +
                        ", ".join(f"{c} ({names[c]})" for c in hits[:10])
                    )
                else:
                    await status.edit_text(f"No service matches '{query}'. Try /services {query}")
                return
        title = f"stock for '{esc(query)}'"
    else:
        picked = merged
        title = "all services with stock"

    ranked = sorted(
        picked.items(),
        key=lambda kv: -sum(c for c, _ in kv[1].values()),
    )
    lines = [f"<b>India · {title}</b>", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    shown = 0
    for code, providers in ranked:
        total = sum(c for c, _ in providers.values())
        if not total and not query:
            continue
        name = names.get(code, "")
        label = f"<code>{esc(code)}</code> {esc(name)}".strip()
        lines.append(f"{label}: <b>{total}</b>  ({esc(fmt_providers(providers))})")
        shown += 1
        if shown >= 40:
            lines.append(f"… {len(ranked) - shown} more; narrow with /stock NAME")
            break

    # Buy buttons: one service -> a button per operator that has stock;
    # overview -> one button per top service using the current operator.
    if len(ranked) == 1 and shown:
        code, providers = ranked[0]
        row: list[InlineKeyboardButton] = []
        for pid, (count, prices) in sorted(providers.items(), key=lambda kv: -kv[1][0]):
            if not count:
                continue
            price = f" @{prices[0]:g}" if prices else ""
            row.append(InlineKeyboardButton(
                f"op{pid} · {count}{price}", callback_data=f"buy:{code}:{pid}"
            ))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    else:
        row = []
        for code, providers in ranked[:8]:
            if not sum(c for c, _ in providers.values()):
                continue
            short = (names.get(code) or code)[:14]
            row.append(InlineKeyboardButton(f"Buy {short}", callback_data=f"buy:{code}:"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    if shown == 0:
        lines.append("nothing in stock")
    if errors:
        lines += ["", "<i>" + esc("; ".join(errors)) + "</i>"]
    if not buttons:
        lines += ["", "buy: <code>/op N</code> then <code>/buy CODE</code>"]
    text = "\n".join(lines)
    await status.edit_text(
        text[:4000], parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


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

    if action == "buy":
        code, _, op = act_id.partition(":")
        await query.answer(f"Buying {code}" + (f" via op{op}" if op else ""))
        await do_buy(context, query.message, code, DEFAULT_COUNTRY, None, op or None)
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
