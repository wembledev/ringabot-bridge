# ringabot-bridge

Open-source phone line for a Grok bot. You bring your own xAI key and your own [Voip.ms](https://voip.ms) number. The bridge places one outbound call at a time, speaks with Grok realtime voice, and exposes that as an MCP server.

Version 1 is a single self-hosted process. It does not ship credentials, phone numbers, or configuration copied from any other voice agent. No secrets in this repo.

## Acceptable use

This is not a dialer for telemarketing, robocalls, or cold outreach. Do not call people who are not expecting a call from you.

- Every call starts with a spoken disclosure that the caller is an AI assistant, not a human. That line is not optional.
- The bridge refuses emergency numbers. It is not a way to reach 911 or any other emergency service. If someone needs help, they must hang up and dial emergency services themselves.
- You stay the Voip.ms account holder. Minutes and the phone number bill to your account.
- Recordings are off by default. If you turn them on, consent rules depend on the province or state. Keep the retention window short.
- There is no bulk-dial endpoint. Destinations have to match your allowlist, and a daily cap plus a concurrency cap apply.

## What you get

One lightweight async HTTP server (Starlette) on `127.0.0.1`. Caddy terminates TLS in front of it. State is a SQLite file (WAL). There is no Redis, queue broker, or separate database server.

Slow work (the call itself, webhook side effects) runs as an in-process task. `POST /call` and `POST /webhook` return `202` before that work finishes. Outbound webhooks use a short timeout (default 2 seconds).

| Tool | What it does |
| --- | --- |
| `connect_number` | Store Voip.ms SIP settings and try a test REGISTER. The password is write-only. |
| `link_voice_agent` | Check an xAI key with a short API call and store it encrypted. The key is write-only. |
| `get_setup_status` | Number configured? Key checked? Current limits. |
| `place_call` | One outbound call. Returns a `call_id`. |
| `get_call` | Status, transcript, summary. |
| `list_calls` | Recent calls. |
| `hang_up` | End one call. |
| `delete_account` | Wipe stored secrets, transcripts, and recordings. |

`set_inbound` is not in v1. Inbound answering needs a registered user agent per number; leave `INBOUND_MODE=off`.

MCP is streamable HTTP at `/mcp` (POST JSON-RPC, `application/json`). `GET /mcp` returns 405. Self-host auth is a static bearer token (`API_TOKEN`), compared in constant time. Hosted OAuth 2.1 can implement the same auth interface later.

Plain HTTP, same token:

- `POST /call` with `{"number","briefing","questions":[],"max_minutes":null}`
- `GET /calls/<id>`, `GET /calls`
- `POST /calls/<id>/hangup` or `POST /hangup`
- `GET /status`, `GET /recordings`, `GET /health`

## Quick start

You need Python 3.12, [uv](https://docs.astral.sh/uv/), and on Linux the `baresip` and `pulseaudio` packages. Calls use 8 kHz PCM so the box mostly just forwards audio.

```bash
uv sync
cp .env.example .env
# fill .env on this machine. do not commit it.
set -a
# shellcheck disable=SC1091
source .env
set +a
uv run ringabot doctor --offline
uv run ringabot serve
```

The server binds `127.0.0.1:8787` unless you change `HOST` and `PORT`. Put Caddy (`deploy/Caddyfile`) on 443 and proxy to that port. Generate `API_TOKEN` with `openssl rand -hex 32`. Generate a 32-byte master key:

```bash
openssl rand -out /etc/ringabot/master.key 32
chmod 0400 /etc/ringabot/master.key
chown ringabot:ringabot /etc/ringabot/master.key
```

`/etc/ringabot/ringabot.env` should be mode `0600` and owned by the service user. `ringabot doctor` checks that file, then (without `--offline`) the xAI key, a SIP REGISTER, and the public TLS handshake.

`AUDIO_MODE=memory` skips PulseAudio. Point `SIP_CTRL` at a fake user agent if you are measuring the process without a sound card or a carrier.

## Voip.ms

Do this in your own Voip.ms account. The screens in that portal move; these are the steps, not a screenshot tour.

1. Create the account and complete whatever identity checks they ask for.
2. Order a DID. Emergency-address service on that number is between you and Voip.ms. This bridge still will not place emergency calls.
3. Create a SIP sub-account. Note the pop hostname, the username, and the password.
4. Route the DID to that sub-account so caller ID matches the number you bought.
5. Put those four values in the env file, or pass them to `connect_number`.

Example values in this repo use fictional `555` numbers such as `+15555550100` and hostnames like `sip.example.com`. Use the hostname Voip.ms shows you, not an example.

If you deployed with the Linode StackScript, the UDF form is stored in Linode metadata. Rotate the SIP password afterwards if you do not want it to stay there. The script writes secrets only to the env file and writes the API URL plus `API_TOKEN` to `/root/ringabot-credentials.txt` (mode `0600`). It does not send them anywhere.

## Connect a Grok bot

Self-host: add an MCP server whose URL is `https://<your-host>/mcp` and whose header is `Authorization: Bearer <API_TOKEN>`. The bot then calls `place_call`, `get_call`, and `hang_up`.

A call looks like:

```json
{
  "number": "+15555550100",
  "briefing": "Confirm tomorrow's picnic time.",
  "questions": ["What time works?"],
  "max_minutes": 5
}
```

The opening line is the AI disclosure, then your greeting if you set one. After the call, `get_call` returns the transcript and a short JSON summary. Idle silence and the max length both say goodbye and hang up.

`link_voice_agent` stores the xAI key only after `GET /v1/models` accepts it. `connect_number` stores the SIP password only in the encrypted SQLite box (AES-GCM under the master key) and never returns it.

## Webhooks

Optional. Set `TOOLS_WEBHOOK_URL` and `TOOLS_WEBHOOK_SECRET` to your own backend. The bridge POSTs JSON when the model calls `notify_backend`, and again with `call.completed`. There is no queue in the middle.

Each outbound request has:

- `X-Ringabot-Timestamp`: unix seconds
- `X-Ringabot-Signature`: `v1=` plus HMAC-SHA256 of `{timestamp}.{raw body}` using the shared secret

Reject timestamps outside `WEBHOOK_REPLAY_WINDOW_SECONDS` (default 300) and reject a signature you have already seen. Compare the MAC and any bearer token with a constant-time compare.

`POST /webhook` on the bridge accepts either that HMAC or `Authorization: Bearer <API_TOKEN>`. It checks the body before doing work, returns `202`, and finishes the action on the same event loop. Unknown or huge bodies are rejected immediately. The timeout on outbound calls stays small so a slow receiver cannot pin the 1 GB host.

```json
{"action": "hangup", "call_id": "call_..."}
{"action": "note", "call_id": "call_...", "text": "operator note"}
```

## Abuse guards

- Destination allowlist (`ALLOWED_DESTINATIONS`, default `+1` when unset).
- Emergency short codes are rejected.
- Daily cap (`MAX_CALLS_PER_DAY`, default 20) per tenant.
- One active call per tenant, and a process-wide cap (`MAX_CONCURRENT_CALLS`, default 4).
- When the cap is full the API returns `429` with `Retry-After`. It does not queue in another service.
- Max call length (`MAX_CALL_SECONDS`, default 600).
- A per-IP rate limit on the HTTP server.
- Logs pass through a redaction filter. Do not log the env file.

`delete_account` (MCP, `confirm: true`) removes stored secrets and call records and stops using the env-file credentials until you link them again through the tools.

## Install on a small VPS

`deploy/stackscript.sh` is a Linode StackScript (Nanode-sized). It installs baresip, PulseAudio, Python, uv, and Caddy, creates a `ringabot` user, writes the `0600` env file, generates `API_TOKEN` and the master key, enables unattended-upgrades, and opens ufw for 22, 80, and 443 only.

A release tarball download requires `RINGABOT_SHA256`. Otherwise the script clones this git repo. `deploy/cloud-init.yaml` is the same shape for other clouds. `deploy/ringabot.service` sets `NoNewPrivileges`, `ProtectSystem=strict`, and `MemoryMax=800M`.

`ringabot doctor` is the check after boot: SIP REGISTER, the xAI key, and TLS.

## Development

```bash
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Tests use a fake SIP user agent and a fake Grok WebSocket on localhost. They do not need a carrier, a key, or outbound network. CI runs ruff, pytest, and gitleaks. `.pre-commit-config.yaml` runs the same secret scan locally.

The persona example is `examples/persona.example.yaml`. Copy it and point `PERSONA_FILE` at your copy.

## License

MIT. See `LICENSE`.
