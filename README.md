# ringabot-bridge

Ringabot is a lightweight listener for [xAI](https://x.ai) voice calls on a Voip.ms SIP line. Inbound calls arrive as a signed webhook. Outbound calls dial out through baresip. An MCP connector on `/mcp` exposes `place_call`, `list_calls`, and `hang_up`, authenticated with `Authorization: Bearer <API_TOKEN>`.

One process, no database. Audio for inbound calls stays on xAI. Outbound audio is relayed on this host between baresip and the xAI realtime websocket. Nothing is recorded.

Built by [Wemble Development Corporation](https://wemble.com), BC, Canada.

## Do it yourself

Prefer not to run a server? A hosted number is $4 USD or $5 CAD a month at [ringabot.com](https://ringabot.com).

1. **Voip.ms number.** Create your own Voip.ms account and order a phone number. In the DID routing settings, send it to the SIP URI `<e164>@sip.voice.x.ai` (your number in E.164, such as `+15555550100@sip.voice.x.ai`). The sub-account used for outbound calls needs a caller ID and device type "IP phone / softphone".

2. **xAI voice agent.** Create your own xAI API key. After step 3 is serving HTTPS, register that number so xAI sends call events to this bridge:

   ```bash
   sudo bash -c 'set -a; . /etc/ringabot/ringabot.env; set +a; \
     /opt/ringabot/.venv/bin/ringabot register-number --name my-line'
   ```

   That command creates the Direct SIP number and webhook, and writes the signing secret into the env file. Then point Voip.ms at `sip:<e164>@sip.voice.x.ai` if you have not already. Set `SIP_ALLOWED_CIDRS` to your carrier's signaling range before you register, so xAI only accepts INVITEs from that range.

3. **Small VPS.** A 1 GB machine is enough (about $5 a month). These commands assume Ubuntu 24.04. Replace `phone.example.com` with your own hostname, and fill `/etc/ringabot/ringabot.env` from `.env.example` (mode `0600`, never commit it).

   ```bash
   sudo apt update
   sudo apt install -y baresip alsa-utils caddy
   curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
   echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf
   sudo modprobe snd-aloop

   sudo useradd --system --home /opt/ringabot --shell /usr/sbin/nologin ringabot
   sudo git clone https://github.com/wembledev/ringabot-bridge /opt/ringabot
   sudo bash -c 'cd /opt/ringabot && uv sync --frozen --no-dev'

   sudo install -d -m 0750 -o root -g ringabot /etc/ringabot
   sudo install -m 0640 -o root -g ringabot /opt/ringabot/.env.example /etc/ringabot/ringabot.env
   sudo chmod 0600 /etc/ringabot/ringabot.env
   sudo chown ringabot:ringabot /etc/ringabot/ringabot.env
   sudoedit /etc/ringabot/ringabot.env

   sudo cp /opt/ringabot/deploy/Caddyfile /etc/caddy/Caddyfile
   sudo systemctl edit caddy
   ```

   In the Caddy drop-in:

   ```ini
   [Service]
   Environment=PUBLIC_HOSTNAME=phone.example.com
   ```

   Caddy terminates HTTPS and proxies only `/xai/webhook`, `/healthz`, and `/mcp`. Admin routes stay on loopback. Open `443/tcp` and `80/tcp`. For outbound audio, allow UDP from your carrier's media range to ports 1024–65535.

   ```bash
   sudo cp /opt/ringabot/deploy/ringabot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now caddy ringabot
   ```

   Generate the bearer token with `openssl rand -hex 32` and put it in `API_TOKEN`.

4. **Grok Bot.** Add the MCP connector. The URL is `https://phone.example.com/mcp`. Send `Authorization: Bearer <API_TOKEN>`.

   ```bash
   grok mcp add --transport http ringabot https://phone.example.com/mcp \
     --header "Authorization: Bearer ${RINGABOT_API_TOKEN}"
   ```

   Tools: `place_call(number, briefing)`, `list_calls(limit)` (outbound calls only), `hang_up(call_id)`. Any MCP client that can send a bearer header can use the same URL.

5. **Check the box.**

   ```bash
   sudo -u ringabot bash -c 'set -a; . /etc/ringabot/ringabot.env; set +a; \
     /opt/ringabot/.venv/bin/ringabot doctor'
   ```

   Green means every line starts with `ok` and the last line is `doctor: clean`: the xAI key is set and accepted, the webhook secret is well formed, `API_TOKEN` is 32 characters or more, `DID` and `PUBLIC_URL` are set, the process is bound to loopback, baresip and snd-aloop are present, `ALLOWED_DESTINATIONS` is set, and the DID is registered with xAI.

6. **First test call.** Put your own mobile number in `ALLOWED_DESTINATIONS`, restart the service, and place one call. It opens by saying it is an AI assistant.

   ```bash
   sudo systemctl restart ringabot
   curl -sS -X POST http://127.0.0.1:8787/call \
     -H "Authorization: Bearer $API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"number":"+15555550100","briefing":"This is a test. Say hello and hang up."}'
   ```

   Or ask Grok Bot to call `place_call` with that number and a short briefing.

Outbound calls only go to numbers matching `ALLOWED_DESTINATIONS`, never to emergency numbers.

## Environment

Copy `.env.example` to `/etc/ringabot/ringabot.env`. Leave a value blank to keep the default.

| Variable | Purpose |
| --- | --- |
| `XAI_API_KEY` | Your xAI API key |
| `XAI_WEBHOOK_SECRET` | Webhook signing secret (`whsec_...`). `register-number` writes this |
| `API_TOKEN` | Bearer token for `/mcp` and the admin routes. 32+ characters |
| `DID` | Your carrier number, E.164 or 10 digits |
| `PUBLIC_URL` | Public `https://` origin Caddy serves |
| `SIP_ALLOWED_CIDRS` | Carrier signaling ranges allowed to INVITE xAI |
| `HOST`, `PORT` | Listener. Default `127.0.0.1:8787` |
| `PERSONA_NAME`, `VOICE`, `INSTRUCTIONS`, `GREETING` | Persona. Defaults: Ringabot, voice `aurora`, brief and polite, greeting that says it is an AI |
| `MAX_CALL_SECONDS`, `IDLE_TIMEOUT`, `MAX_CONCURRENT_CALLS` | Defaults: 600, 30, 2 |
| `SIP_SERVER`, `SIP_USERNAME`, `SIP_PASSWORD` | Carrier sub-account for outbound dialing |
| `ALLOWED_DESTINATIONS` | Comma-separated E.164 prefixes you may call |

## Run and test

Locally:

```bash
uv sync --all-groups
uv run ringabot serve
```

`ringabot doctor` checks configuration. `ringabot register-number` registers the DID.

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## HTTP

| Route | Auth | What |
| --- | --- | --- |
| `POST /xai/webhook` | xAI signature | Incoming-call events (public via Caddy) |
| `GET /healthz` | none | Liveness (public via Caddy) |
| `POST /mcp` | bearer | MCP connector (public via Caddy) |
| `POST /call` | bearer | Place one outbound call (loopback only) |
| `GET /calls` | bearer | Recent calls and transcripts (loopback only) |
| `POST /calls/{id}/hangup` | bearer | End a call (loopback only) |

Webhooks are checked with Standard Webhooks v1 (HMAC-SHA256 over `id.timestamp.body`), a 5 minute window, and a replay cache. Calls to any number other than `DID` are refused.
