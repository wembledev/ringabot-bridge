# ringabot-bridge

A tiny webhook listener for **xAI Grok Voice Direct SIP**. You bring your own carrier
number (for example from Voip.ms) and your own xAI key. Calls go straight from the carrier
to xAI over SIP; this process never touches audio. It only:

1. receives xAI's signed `realtime.call.incoming` webhook and answers it in milliseconds,
2. joins the call with `wss://api.x.ai/v1/realtime?call_id=...` in a background task,
   sends the persona (`session.update`) and the opening line,
3. runs the agent's function tools (`end_call`, `get_current_time`, `transfer_call`, plus
   any tools you forward to your own HTTPS endpoint), and keeps a short in-memory
   transcript of recent calls.

One process, no database, no queue. It idles at a few tens of MB and fits a 1 GB VM.
No credentials ship in this repo; `.env.example` is an empty template.

```
caller ──PSTN──> carrier DID ──SIP/TLS──> sip:+1NXXNXXXXXX@sip.voice.x.ai
                                               │ signed webhook
                                               ▼
                         Caddy (TLS) ──> ringabot :8787 (loopback)
                                               │ websocket by call_id (JSON events only)
                                               ▼
                                         xAI realtime session
```

## Acceptable use

Not a dialer for telemarketing or robocalls. The default greeting says the caller is
talking to an AI assistant. Recordings are not made; transcripts live only in memory
(last `KEEP_CALLS` calls) and are visible only with the admin token.

## Install (Ubuntu 24.04)

```bash
sudo useradd --system --home /opt/ringabot ringabot
sudo git clone https://github.com/wembledev/ringabot-bridge /opt/ringabot
cd /opt/ringabot && sudo uv sync --frozen --no-dev
sudo install -d -m 0750 -o root -g ringabot /etc/ringabot
sudo install -m 0600 -o ringabot .env.example /etc/ringabot/ringabot.env   # then fill it
sudo apt install caddy && sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # set PUBLIC_HOSTNAME
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

Only `443/tcp` (and `80/tcp` for the ACME challenge) need to be open. No SIP or RTP ports
are needed on this host.

## Security

- Every webhook is verified with Standard Webhooks v1 (HMAC-SHA256 over
  `id.timestamp.body`), a 5 minute timestamp window and a replay cache.
- Optional `XAI_WEBHOOK_BEARER` is registered with xAI and must match too.
- Calls to any number other than `DID` are refused. `MAX_CONCURRENT_CALLS` caps load.
- Admin routes need `API_TOKEN`. The listener binds to loopback; Caddy only proxies the
  routes it needs.
- Forwarded tools are POSTed with the same signed header scheme using
  `TOOLS_FORWARD_SECRET`, with a short timeout so the agent never stalls.

## HTTP

| Route | Auth | What |
| --- | --- | --- |
| `POST /xai/webhook` | xAI signature | Incoming-call events |
| `GET /healthz` | none | Liveness and active call count |
| `GET /calls` | bearer | Recent calls, transcripts, tool calls |
| `POST /calls/{id}/hangup` | bearer | End a call |
| `POST /calls/{id}/transfer` | bearer | Dial out from a live call to a configured target (SIP REFER) |
| `POST /calls/outbound` | bearer | Returns 501: xAI has no call-origination API yet |

## Outbound calls

xAI's API (as of October 2026) documents inbound Direct SIP plus `refer` and `hangup` for
live calls, but no endpoint to originate a call. Dial-out therefore works only from a live
call (`transfer_call` tool or `/calls/{id}/transfer`). `/calls/outbound` is reserved for
when origination lands.

## Develop

```bash
uv sync --all-groups && uv run ruff check . && uv run pytest
```
