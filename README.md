# TemporaSMS Telegram bot

Single-operator bot over the TemporaSMS API. Buys a virtual number, polls for
the OTP, delivers it to your chat, and keeps the activation open so you can
request a second SMS on the same number.

Locked to one Telegram user id. Your API key spends real money, so there is no
multi-user path in this build.

## Files

| file | what it does |
|---|---|
| `bot.py` | handlers, buttons, poller, startup/shutdown |
| `tempora.py` | async API client, response parsing, rate limiter |
| `store.py` | SQLite persistence for activations |

## Deploy on Railway

1. Push these files to a repo, create a Railway service from it.
2. Railway autodetects Python and installs `requirements.txt`. The `Procfile`
   runs it as a **worker** — no port, no healthcheck needed.
3. Set the variables from `.env.example` under **Variables**.
4. For history that survives redeploys, attach a Volume mounted at `/data`
   and set `DB_PATH=/data/bot.db`. Without it the DB resets on each deploy;
   live activations are still rehydrated within a single container lifetime.

`ADMIN_ID` is your numeric Telegram id — message @userinfobot to get it.

## Commands

```
/number [MAXPRICE]                catch-all number, no app asked (aliases /num /get)
/any [CODE]                       show or set the catch-all service /number buys
/buy SERVICE [MAXPRICE]           number for one app, e.g. /buy wa
                                  (COUNTRY arg appears after SERVICE when LOCK_COUNTRY=0)
/active                           live activations with buttons
/recent                           last 15
/balance                          wallet balance
/price SERVICE
/stock [NAME|CODE]                live stock per operator; no arg = every service
/op [N|smart|auto|cheap|best]     show or switch the active operator
/operators                        list upstream operator ids
/countries
/services [filter]                filter is a substring match
/stats
```

Each activation card has four buttons:

- **Another SMS** — `setStatus 3`. Same service only.
- **Refresh** — re-render without waiting for the poll.
- **Done** — `setStatus 6`, with a confirm step. Releases the number for good.
- **Cancel + refund** — `setStatus 8`. Refused once a code has arrived.

## Behaviour worth knowing

**The activation is never auto-finished after the first code.** Most naive
implementations call `setStatus 6` as soon as a code lands, which releases the
number and kills any chance of a second SMS. This one holds the activation
until you press Done or the window expires.

**Expiry is handled two ways.** A window that expires with no code is
auto-cancelled, which is what triggers the refund to your wallet. One that
expires with codes is finished cleanly.

**One poller, not one per activation.** All live activations are swept in a
single repeating job through a shared rate limiter, so the per-key limit
holds regardless of how many numbers are open. A `TOO_MANY_REQUESTS` skips the
rest of that cycle rather than hammering.

**Codes are deduplicated** by value. If the same code legitimately arrives
twice, the second is not re-sent — change the check in `poll_activations` if
that matters for your use.

## Verify these against your account

Two things I could not confirm from the public docs, both one cheap test away:

1. **`ACTIVATION_TTL`** defaults to 1200s (20 min), which is the convention for
   this API family, not something TemporaSMS documents. Buy one number, see
   when it actually dies, set the variable to match.

2. **Whether `setStatus 3` costs anything.** On this protocol the retry is
   normally included in the activation price. Test: buy a cheap number, take
   the first code, press Another SMS, trigger a second, and compare
   `/balance` before and after. That answers whether "two codes" costs you
   one activation or two.

Also confirm the service and country codes you need with `/services` and
`/countries` — the short codes vary between providers.

## Scope

This build is for your own accounts and your own SMS flows. TemporaSMS's
Acceptable Use prohibits evading third-party platform rules or creating
accounts against their terms, and states that abusive keys may be suspended,
revoked, or reported. That exposure sits on your key.
