# CLEX_OTP — project handoff

Context transfer for an agent picking this up cold. Written 2026-09-21.

---

## 1. Identity

| | |
|---|---|
| Project | **CLEX_OTP** — Telegram bot over the TemporaSMS virtual-number API |
| Owner | Bhabani (GitHub `CLEXER17`), Bhubaneswar, India, IST |
| Repo | `https://github.com/CLEXER17/CLEX_OTP` |
| Local path | `C:\Users\bhaba\CLEX OTP` (Windows) |
| Hosting | Railway, as a **worker** service (no port, no healthcheck) |
| Language | Python 3.11+, `python-telegram-bot[job-queue]==21.9`, `httpx` |
| Brand | Part of the CLEXER family (sibling project: `CLEXER17/CLEXER_BOT`, a BTC signal bot — unrelated codebase, do not mix them) |

---

## 2. Goal, and the scope boundary

**What it does:** buys a temporary virtual number from Bhabani's own
TemporaSMS wallet, polls for the OTP, and delivers codes to his Telegram
chat — keeping the activation open so a second SMS can be requested on the
same number.

**What was actually built: a single-operator tool.** Locked to one
`ADMIN_ID`. No user accounts, no internal balances, no payments.

**What was asked for first, and declined:** a public reseller bot — anonymous
Telegram users top up with crypto, get assigned numbers, pay per OTP at a
markup. That was declined, and an agent picking this up should not quietly
build it. The reasoning, so it doesn't get relitigated from scratch:

- TemporaSMS's own Acceptable Use prohibits "evading third-party platform
  rules or creating accounts against their terms," and states abusive keys
  may be suspended, revoked, **or reported**. A reseller carries every
  customer's behaviour under one key.
- In India, reselling OTP numbers to anonymous buyers is an actively
  prosecuted pattern (IT Act 66C/66D). The trace in a UPI/bank fraud runs
  victim → mule → number → bot → operator's Telegram and crypto wallet. The
  India-based operator is the reachable node; the provider is a Wyoming LLC.
- No Indian payment gateway onboards this, so it forces USDT — no dispute
  process, and the wallet is the paper trail.

If Bhabani raises reselling again: a closed bot for known customers with a
service blocklist (no banking, UPI, payment, or government services) is a
different conversation worth having. An anonymous public one is not.

---

## 3. The provider — TemporaSMS

Base URL: `https://api.temporasms.com/stubs/handler_api.php`
Auth: `api_key` parameter on **every** call. There is **no login endpoint**
and no session — the key is the whole auth model. Bhabani already has an
account; the key comes from the website, manually, once.

**Full documented endpoint list** (this is all of it):
`getBalance`, `getOperators`, `getNumber`, `getNumberV2`, `getSMS`,
`getSMS V2 (batch)`, `setStatus`, `getPrices`, `getPrices V2`,
`getPrices V3`, `getCountries`, `getServices`.

**Two things that are absent and shaped the whole design:**

1. **No rental API.** No `getRentNumber` / `continueRent`. 5SIM and
   SMS-Activate both have one; TemporaSMS is activation-only. This is why
   "hold a number for days, receive unlimited SMS" is not on the table.
2. **No funding endpoint.** No `addFunds` / `topUp` / deposit. Wallet
   top-ups happen in a browser on their site, by Bhabani, manually. The bot
   cannot and should not attempt this.

It is an SMS-Activate-protocol clone, so v1 responses are bare text, not JSON.

### Response shapes

```
ACCESS_BALANCE:100.1234        getBalance
ACCESS_NUMBER:<id>:<phone>     getNumber
STATUS_WAIT_CODE               getStatus — waiting for first SMS
STATUS_WAIT_RETRY:<lastcode>   getStatus — code seen, another requested
STATUS_OK:<code>               getStatus — code received
STATUS_CANCEL                  getStatus — cancelled
```

Errors arrive in place of a result: `BAD_ACTION`, `BAD_KEY`, `USER_BANNED`,
`ERROR`, `TOO_MANY_REQUESTS`, `UNDER_DEVELOPMENT` (documented), plus
`NO_NUMBERS`, `NO_BALANCE`, `NO_ACTIVATION`, `BAD_SERVICE`, `BAD_STATUS`,
`EARLY_CANCEL_DENIED` (protocol-standard, handled defensively).

### setStatus values

| value | meaning |
|---|---|
| `1` | number in hand, OTP about to be triggered |
| `3` | **request another SMS on the same activation** |
| `6` | finish — **irreversible**, releases the number to the pool |
| `8` | cancel + refund — only works before any SMS arrives |

---

## 4. Domain knowledge that is easy to get wrong

**An activation is one number, bound to one service, for one window.**
`getNumber?service=wa` gives a number that only works for WhatsApp. A
different app needs a new activation and will be a different number. This
killed the original product idea of "user keeps one number and pays per OTP
across apps" — it is not possible on this API.

**`setStatus 3` is a resend within the live window, not a new purchase.**
On this protocol it is normally included in the activation price. So two
codes typically cost one activation. Any pricing model built on "charge per
OTP" is charging twice for one cost — legitimate as markup, but the unit
economics must be understood, not assumed.

**`setStatus 6` is the trap.** Naive implementations call it as soon as the
first code lands, which releases the number and makes a second SMS
impossible. This bot deliberately never does that. See §6.

---

## 5. Architecture

```
bot.py          handlers, inline buttons, the poller job, startup/shutdown
tempora.py      async API client, response parsing, shared rate limiter
store.py        SQLite persistence for activations
test_client.py  offline tests — no network, no Telegram (34 checks)
requirements.txt / Procfile / .env.example / .gitignore / README.md
```

### Commands

```
/buy SERVICE COUNTRY [MAXPRICE]   e.g. /buy wa 0
/active      live activations, each with buttons
/recent      last 15
/balance     wallet balance
/price SERVICE [COUNTRY]
/countries
/services [filter]
/stats       local counters
```

### Buttons on each activation card

`Another SMS` (setStatus 3) · `Refresh` · `Done` (setStatus 6, with a
confirm step) · `Cancel + refund` (setStatus 8, refused once a code exists).

---

## 6. Design decisions — do not break these

1. **The activation is never auto-finished after the first code.**
   `setStatus 6` fires only on a confirmed Done press or on window expiry.
   This is the single most important behaviour in the codebase and it is
   covered by a test named `STILL LIVE, never auto-finished`.
2. **Expiry splits two ways.** Expired with no code → `setStatus 8` to
   trigger the provider-side refund. Expired with codes → clean
   `setStatus 6`. Losing the auto-cancel path means silently losing money.
3. **One poller for all activations**, not one job per activation, through a
   shared rate limiter in `tempora.py`. `TOO_MANY_REQUESTS` skips the rest
   of that cycle rather than hammering.
4. **Codes are deduped by value** so repeated polls don't double-notify.
   Side effect: a genuinely repeated identical code is suppressed.
5. **Admin lock on every handler and every callback query**, not just
   commands. The API key spends real money.
6. **Live activations rehydrate from SQLite on boot**, so a Railway redeploy
   mid-activation doesn't orphan a number that was already paid for.

---

## 7. Environment variables

Required — the bot exits at startup naming any that are missing:

```
BOT_TOKEN=            # @BotFather
TEMPORASMS_API_KEY=   # temporasms.com account page
ADMIN_ID=             # numeric Telegram id, via @userinfobot
```

Optional, with defaults:

```
DB_PATH=./bot.db      # on Railway: /data/bot.db + mounted volume
DEFAULT_COUNTRY=0
POLL_INTERVAL=5
ACTIVATION_TTL=1200
MAX_LIVE=5
```

Railway: Variables → Raw Editor. Volume mounted at `/data`, or
`DB_PATH=/data/bot.db` fails to write on boot.

---

## 8. State as of this handoff

**Done:** all code written, 53 checks passing (34 parser/store in
`test_client.py`, 19 poller-state-machine in an ad-hoc harness). Files
committed to `main` in `C:\Users\bhaba\CLEX OTP` with remote set to the
GitHub repo. Working tree clean, `git fsck` clean.

**Pending:**

- **The push has not happened.** The commit exists locally; Bhabani runs
  `git push -u origin main` from Windows. The cloud session's token is
  scoped to `CLEXER17/CLEXER_BOT` only and the git proxy refuses `CLEX_OTP`;
  the Linux VM on his machine has no GitHub credentials. Do not ask him to
  paste a PAT into chat.
- **Never run against the live API.** Every test is offline with a faked
  transport. Service and country codes (`wa`, `0`) are guesses until
  confirmed with `/services` and `/countries`.

### Two open questions, both one cheap live test away

1. **Real activation window.** `ACTIVATION_TTL` defaults to 1200s because
   that is the protocol convention, not because TemporaSMS documents it.
   Buy one number, observe when it actually dies, set the variable.
2. **Whether `setStatus 3` costs anything.** Buy a cheap number, take the
   first code, press Another SMS, trigger a second, compare `/balance`
   before and after. This is the number any future pricing rests on.

### Offered but not yet built

A balance guard: pre-flight price-vs-balance check before `/buy`, a
low-balance alert DM below a threshold, and post-purchase balance display.
Bhabani was asked and hasn't answered yet.

---

## 9. Working with Bhabani

Stated preferences: direct and minimal responses; copy-paste-ready output;
complete files over partial diffs; precise answers over broad overviews.

He builds in Python and deploys on Railway — this matches his CLEXER_BOT
setup, so lean on those conventions. Technically capable; skip the
hand-holding, keep the caveats short and load-bearing.

Deletion is enabled for the `CLEX OTP` folder in the current session
because git cannot operate without removing its own lock files. A fresh
session has to request that again.
