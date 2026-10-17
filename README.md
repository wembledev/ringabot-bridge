# ringabot-bridge

Small phone line for a Grok voice agent on a 1 GB VM. You bring your own carrier number
(for example Voip.ms) and your own xAI key.

- **Inbound:** the carrier routes the DID straight to xAI Direct SIP. xAI POSTs a signed
  `realtime.call.incoming` webhook; we answer it in milliseconds, then join the call with
  `wss://api.x.ai/v1/realtime?call_id=...` in a background task. No audio touches this host.
- **One persona:** the webhook server sets voice, instructions, opening line and tools for
  every call (`session_config`); outbound only adds a briefing. Every call
  opens with a short greeting (default "Hi, this is Ringabot, an AI assistant.") as a normal
  response that xAI can interrupt like any other turn; the full AI and emergency rules stay
  in the instructions. Inbound
  answers first; outbound waits for the callee's hello (greets anyway after 4 s).
- **Outbound:** `POST /call` starts one baresip process that dials through the carrier
  sub-account; 8 kHz PCM is relayed between snd-aloop and the xAI realtime websocket.
- **Hang-up:** the agent says goodbye, then calls `end_call`. We return a bare
  `{"ok": true}` without asking for a new response (so nothing else is spoken), wait until
  the goodbye has played plus a short tail, and send exactly one hang-up (xAI call
  control for inbound, baresip for outbound).
- **Turn-taking:** xAI's server VAD owns interruptions. On outbound we only drop the speech
  we have queued for the phone when xAI reports `speech_started`. A 10 s backstop, an idle timeout and a max
  call length end anything that lingers.

One process and one sqlite file for accounts and numbers. No queue. No credentials ship
in this repo. `.env.example` lists names only.

```
caller ──PSTN──> carrier DID ──SIP/TLS──> sip:+1NXXNXXXXXX@sip.voice.x.ai
                                               │ signed webhook
                                               ▼
                         Caddy (TLS) ──> ringabot :8787 (loopback)
                                               │ websocket by call_id (JSON events only)
                                               ▼
                                         xAI realtime session
```

## Connect your Grok Bot (MCP)

The server exposes a small MCP connector at `https://<PUBLIC_HOSTNAME>/mcp` (Streamable HTTP,
stateless). Tools: `place_call(number, briefing)`, `list_calls(limit)`, `hang_up(call_id)`,
`get_number(currency, area_code?)`.
It uses the same guards as `POST /call` (allow list, no emergency numbers, AI greeting).
`list_calls` returns outbound calls only.

Auth is `Authorization: Bearer <token>` (no OAuth). `API_TOKEN` is the admin token for
`/call`, `/calls`, and operator use of MCP. Each Grok Bot account uses its own token from
`POST /signup`. The server stores only the SHA-256 hash. `place_call` and `list_calls`
for an account token require a number in status `active`, and they see only that account's
calls. The admin token keeps the previous behavior and does not need a paid number.

```bash
# Grok CLI
grok mcp add --transport http ringabot https://<PUBLIC_HOSTNAME>/mcp \
  --header "Authorization: Bearer ${RINGABOT_API_TOKEN}"
```

With the xAI API, pass the token as `authorization` on an `mcp` tool. Any MCP client that
can send a bearer header works.

## Buy a number

`POST /signup` creates an account and returns `api_token` once. Put that token on the
connector. The bot then calls `get_number` with `currency` `usd` or `cad` and an optional
`area_code`. The tool returns a Stripe Checkout URL. The user opens it and approves one
monthly subscription. Intended prices, once they exist in the dashboard, are $4 USD a
month and $5 CAD a month.

Checkout looks up the active monthly recurring price on `STRIPE_PRODUCT_ID` (default
`prod_VSVWjrAodSEFv9`, the Wemble product on account `acct_1PJio5Ke24N6hhcu`) for the
requested currency. A match is cached for 60 seconds. Set `STRIPE_PRICE_USD` or
`STRIPE_PRICE_CAD` to skip the lookup for that currency. If no monthly price matches,
`get_number` returns an error and does not charge.

`POST /stripe/webhook` checks `Stripe-Signature` with `STRIPE_WEBHOOK_SECRET`.
`checkout.session.completed` sets the number to `active`, keeps `e164` empty, stores the
subscription id, and calls the provisioning hook. `customer.subscription.deleted` sets
`cancelled`. A repeated event does not provision twice, and a completion after cancel
does not turn the number back on.

The hook in this repo only logs. It does not order a Voip.ms number or create a
sub-account. That waits for Michael's OK. The real hook will create a Voip.ms
sub-account, order a DID in the area code from Checkout metadata, route the DID to
xAI SIP `<e164>@sip.voice.x.ai`, and write `e164` on the number row. Until then, an
active number is the billing gate for `place_call`. Outbound audio still uses the
host SIP account.

The server refuses to start when `STRIPE_SECRET_KEY` starts with `sk_live_` unless
`RINGABOT_LIVE_CHARGES=1`.

```
accounts(id, api_token_hash, email, stripe_customer_id, created_at)
numbers(id, account_id, e164, status, stripe_subscription_id, currency, created_at)
```

`status` is `pending_payment`, `active`, or `cancelled`. `e164` stays null until the
real provisioning hook runs. The sqlite file defaults to
`/var/lib/ringabot/ringabot.sqlite` (`RINGABOT_DB`). The systemd unit's
`StateDirectory=ringabot` is what makes that path writable under `ProtectSystem=strict`.

## Acceptable use

Not a dialer for telemarketing or robocalls. Outbound calls only go to numbers matching
`ALLOWED_DESTINATIONS`, never to emergency numbers, and open with a line saying the caller
is an AI assistant. Nothing is recorded; transcripts live only in memory (last
20 calls) behind the admin token.

## Install (Ubuntu 24.04)

```bash
sudo useradd --system --home /opt/ringabot ringabot
sudo git clone https://github.com/wembledev/ringabot-bridge /opt/ringabot
cd /opt/ringabot && sudo uv sync --frozen --no-dev
sudo install -d -m 0750 -o root -g ringabot /etc/ringabot
sudo install -m 0600 -o ringabot .env.example /etc/ringabot/ringabot.env   # then fill it
sudo apt install caddy && sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # set PUBLIC_HOSTNAME
sudo apt install baresip alsa-utils && echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf
sudo cp deploy/ringabot.service /etc/systemd/system/
```

Then register your number with xAI. This creates the Direct SIP number and its webhook,
and writes the one-time signing secret into the env file without printing it:

```bash
sudo bash -c 'set -a; . /etc/ringabot/ringabot.env; set +a; \
  /opt/ringabot/.venv/bin/ringabot register-number --name my-line'
sudo systemctl enable --now ringabot
sudo -u ringabot bash -c 'set -a; . /etc/ringabot/ringabot.env; set +a; \
  /opt/ringabot/.venv/bin/ringabot doctor'
```

Finally point the carrier at xAI. In Voip.ms: DID → Routing → SIP URI
`+1NXXNXXXXXX@sip.voice.x.ai`. Set `SIP_ALLOWED_CIDRS` to your carrier's signaling range
before registering so xAI only accepts INVITEs from it.

Open `443/tcp` and `80/tcp` (ACME) for the webhook. For outbound calls also allow UDP
from your carrier's media range (Voip.ms: its POP subnet) to ports 1024-65535.

For outbound on Voip.ms, the sub-account needs a caller ID number set and device type
"IP phone / softphone"; otherwise the call goes out with the sub-account name as caller
ID and fails immediately.

## Security

- Every xAI webhook is verified with Standard Webhooks v1 (HMAC-SHA256 over
  `id.timestamp.body`), a 5 minute timestamp window and a replay cache.
- `POST /stripe/webhook` is verified with `Stripe-Signature` and `STRIPE_WEBHOOK_SECRET`.
- Account tokens are stored as SHA-256 hashes. `POST /signup` is the only response that
  contains a token.
- Calls to any number other than `DID` are refused. `MAX_CONCURRENT_CALLS` caps load.
- Admin routes need `API_TOKEN` and are not proxied by Caddy. Call them over SSH.

## HTTP

| Route | Auth | What |
| --- | --- | --- |
| `POST /xai/webhook` | xAI signature | Incoming-call events (public via Caddy) |
| `GET /healthz` | none | Liveness and active call count (public via Caddy) |
| `POST /call` | admin bearer | `{"number","briefing"}`: place one outbound call (same persona as inbound plus the briefing) |
| `GET /calls` | admin bearer | Recent calls with transcripts |
| `POST /calls/{id}/hangup` | admin bearer | End a call |
| `POST /mcp` | admin or account bearer | MCP tools, public via Caddy |
| `POST /signup` | none | Create an account and return its bearer token once |
| `POST /stripe/webhook` | Stripe signature | Activate or cancel a number |
| `GET /billing/success` | none | Page Stripe sends the user to after Checkout |
| `GET /billing/cancel` | none | Page Stripe sends the user to if they back out |

## Develop

```bash
uv sync --all-groups && uv run ruff check . && uv run pytest
```
