# ringabot-bridge

Small phone line for a Grok voice agent on a 1 GB VM. You bring your own carrier number
(for example Voip.ms) and your own xAI key.

- **Inbound:** the carrier routes the DID straight to xAI Direct SIP. xAI POSTs a signed
  `realtime.call.incoming` webhook; we answer it in milliseconds, then join the call with
  `wss://api.x.ai/v1/realtime?call_id=...` in a background task. No audio touches this host.
- **One persona:** the webhook server sets voice, instructions, opening line and tools for
  every call (`session_config`, `opening_event`); outbound only adds a briefing. Every call
  opens with "Hello! I am an AI assistant, not a human. I cannot contact emergency
  services." as a normal response that xAI can interrupt like any other turn. Inbound
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

One process, no database, no queue. No credentials ship in this repo; `.env.example` is
an empty template.

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

Not a dialer for telemarketing or robocalls. Outbound calls only go to numbers matching
`ALLOWED_DESTINATIONS`, never to emergency numbers, and open with a line saying the caller
is an AI assistant. Nothing is recorded; transcripts live only in memory (last
`KEEP_CALLS` calls) behind the admin token.

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

- Every webhook is verified with Standard Webhooks v1 (HMAC-SHA256 over
  `id.timestamp.body`), a 5 minute timestamp window and a replay cache.
- Optional `XAI_WEBHOOK_BEARER` is registered with xAI and must match too.
- Calls to any number other than `DID` are refused. `MAX_CONCURRENT_CALLS` caps load.
- Admin routes need `API_TOKEN` and are not proxied by Caddy; call them over SSH.

## HTTP

| Route | Auth | What |
| --- | --- | --- |
| `POST /xai/webhook` | xAI signature | Incoming-call events (public via Caddy) |
| `GET /healthz` | none | Liveness and active call count (public via Caddy) |
| `POST /call` | bearer | `{"number","briefing"}`: place one outbound call (same persona as inbound plus the briefing) |
| `GET /calls` | bearer | Recent calls with transcripts |
| `POST /calls/{id}/hangup` | bearer | End a call |

## Develop

```bash
uv sync --all-groups && uv run ruff check . && uv run pytest
```
