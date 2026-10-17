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
| `get_setup_status` | Number configured? Key checked? Inbound answering? Current limits. |
| `set_inbound` | Enable or disable auto-answer on the DID. Optional greeting, instructions, and caller allow-list. |
| `place_call` | One outbound call. Returns a `call_id`. |
| `get_call` | Status, transcript, summary. |
| `list_calls` | Recent calls. |
| `hang_up` | End one call. |
| `delete_account` | Wipe stored secrets, transcripts, and recordings. |

`set_inbound` turns on a long-lived baresip registration (`answermode=auto`) for `VOIPMS_DID`. Inbound calls use the same AI disclosure, idle timeout, max length, goodbye, summary, daily cap, and one-active-call limit as outbound calls. Leave `INBOUND_MODE=off` until the DID is routed and `ringabot doctor` is clean.

MCP is streamable HTTP at `/mcp` (POST JSON-RPC, `application/json`). `GET /mcp` returns 405. Self-host auth is a static bearer token (`API_TOKEN`), compared in constant time. Hosted OAuth 2.1 can implement the same auth interface later.

Plain HTTP, same token:

- `POST /call` with `{"number","briefing","questions":[],"max_minutes":null}`
- `GET /calls/<id>`, `GET /calls`
- `POST /calls/<id>/hangup` or `POST /hangup`
- `GET /status`, `GET /recordings`, `GET /health`

## Quick start

You need Python 3.12, [uv](https://docs.astral.sh/uv/), and on Linux the `baresip` and `alsa-utils` packages. Calls use 8 kHz PCM so the box mostly just forwards audio.

Ubuntu 24.04's apt package is baresip 1.0.0. `baresip -n` without an interface name exits immediately, so the bridge does not pass `-n`. `pulse.so` is not in `baresip-core` (it ships in `baresip-x11`, which `--no-install-recommends` does not install) and it still needs a PulseAudio daemon. Audio uses the kernel `snd-aloop` device and the `alsa.so` from the core package: baresip plays and captures on `hw:Loopback,0,N`, and the bridge runs `arecord` / `aplay` on `hw:Loopback,1,N` at 8 kHz, 16-bit, mono, with a 20 ms period. That is one copy in the kernel and no Pulse daemon, which matters on a 1 GB Nanode. `aubridge` never leaves the baresip process, `aufile` is a WAV file rather than a live duplex stream, and building a newer baresip is a compiler and a lot of RAM at install time. `AUDIO_MODE=pulse` is used only when `pulse.so` is on disk; otherwise it is treated as `alsa`. `AUDIO_MODE=memory` skips the sound device.

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

Point `SIP_CTRL` at a fake user agent if you are measuring the process without a sound card or a carrier. `SIP_USERNAME` is the Voip.ms sub-account. `VOIPMS_DID` is only the caller ID. Values in `/etc/ringabot/ringabot.env` are double-quoted so a persona name with spaces survives systemd.

## Voip.ms

Do this in your own Voip.ms account. The screens in that portal move; these are the steps, not a screenshot tour.

1. Create the account and complete whatever identity checks they ask for.
2. Order a DID (a phone number). Emergency-address service on that number is between you and Voip.ms. This bridge still will not place emergency calls.
3. Create a SIP sub-account. Main account → Sub Accounts → Create Sub Account. Note the pop hostname (the SIP server), the username, and the password. Use a city pop close to the VPS.
4. Route the DID to that sub-account. DID Numbers → Manage DIDs → the number → Routing. Choose the sub-account, not a voicemail box or an IVR. Inbound calls then land on the registered user agent. Set the DID's caller ID to the same number so outbound calls show that caller ID.
5. Put the pop hostname, sub-account username (`SIP_USERNAME` or `VOIPMS_SIP_USER`), password, and DID (`+` and country code, `VOIPMS_DID`) in the env file, or pass them to `connect_number`. The username is the REGISTER identity. The DID is not.

Example values in this repo use fictional `555` numbers such as `+15555550100` and hostnames like `sip.example.com`. Use the hostname Voip.ms shows you, not an example.

If you deployed with the Linode StackScript, the UDF form is stored in Linode metadata. Rotate the SIP password afterwards if you do not want it to stay there. The script writes secrets only to the env file and writes the API URL plus `API_TOKEN` to `/root/ringabot-credentials.txt` (mode `0600`). It does not send them anywhere.

## First test call

Checklist for a fresh Linode created from `deploy/stackscript.sh`. Do the Voip.ms steps above before you expect the phone to ring.

1. Deploy a Nanode (or similar) with the StackScript. Fill the UDF fields: xAI key, Voip.ms SIP server, sub-account username, sub-account password, and DID. Optional: public hostname, webhook, allowed destination prefixes.
2. In Voip.ms, create the sub-account and route the DID to that sub-account (step 4 above). The StackScript does not click through the Voip.ms portal for you.
3. SSH to the Linode. Confirm `/etc/ringabot/ringabot.env` is mode `0600` and owned by `ringabot`. The MCP URL and bearer token are in `/root/ringabot-credentials.txt`.
4. Run doctor before any call. It checks the API token, the master key, the env file mode, SIP REGISTER, the xAI key, TLS, and whether inbound answering is on.

   ```bash
   sudo -u ringabot /opt/ringabot/.venv/bin/ringabot doctor
   ```

   `ringabot doctor --offline` checks local files only. Fix any line that says `fail` before you dial. A clean inbound line looks like `ok inbound enabled for +15555550100` once answering is on. While it is still off, doctor reports `ok inbound off`.
5. Turn answering on. Either set `INBOUND_MODE=on` in the env file and `systemctl restart ringabot`, or call MCP `set_inbound` on the running server (no restart):

   ```json
   {"enabled": true, "greeting": "I have a short question.", "instructions": "Help the caller.", "allow": []}
   ```

   `allow` is optional. Leave it empty to answer any caller. Pass E.164 numbers such as `["+15555550123"]` to only answer those callers. Anyone else is hung up and is not connected to the voice agent. `get_setup_status` then shows `inbound.enabled` and `inbound.registered`.
6. Call the DID from a phone. baresip auto-answers. The first thing spoken is the AI disclosure (and a recording clause if `RECORDINGS=on`), then the greeting. The call is stored in SQLite. When it ends, the bridge says goodbye, writes the summary, and POSTs `call.completed` if a webhook is configured. `get_call` returns the transcript.
7. Optional outbound check: `place_call` to a number on `ALLOWED_DESTINATIONS` that is expecting you. The same disclosure, idle timeout, max length, and summary apply. Inbound and outbound share the daily cap and the one-active-call limit.

## Check a box that would not register

On the Nanode, after pulling this branch (or re-running the StackScript against it):

```bash
sudo modprobe snd-aloop
grep Loopback /proc/asound/cards
sudo usermod -aG audio ringabot
sudo systemctl restart ringabot
sudo -u ringabot /opt/ringabot/.venv/bin/ringabot doctor
```

`/etc/ringabot/ringabot.env` should quote values (`PERSONA_NAME="Example Assistant"`) and set `AUDIO_MODE=alsa`. `SIP_USERNAME` is the sub-account. In the live account file under `/var/lib/ringabot/run/baresip-*/accounts`, the URI is `sip:<username>@<pop>` and the DID is only the display name in quotes. `ps -C baresip` should keep showing one process; the old build started a new one about every 10 seconds because it never saw `REGISTER_OK`. `journalctl -u ringabot -n 50` should not be a restart loop.

`sip_listen` is `0.0.0.0:0`, so the contact port changes each time baresip starts. Replies to REGISTER match the existing UDP flow. A test call that rings but stays silent is usually RTP from the pop, which is a new UDP flow; allow that pop if the firewall drops it. Ports 80 and 443 are opened only when `PUBLIC_HOSTNAME` is set.

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

The URL is resolved again at send time. It must be `http` or `https` with no userinfo, and every address it returns has to be a public unicast address. Loopback, private, link-local, and cloud-metadata targets are refused, and the TCP connection is pinned to an address that passed that check. Redirects are not followed. The process keeps one shared HTTP client with a short timeout and stops reading a response after 64 KiB.

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
- Daily cap (`MAX_CALLS_PER_DAY`, default 20) per tenant. Inbound answers count toward the same cap.
- One active call per tenant, and a process-wide cap (`MAX_CONCURRENT_CALLS`, default 4). A second inbound call while a call is up is hung up.
- Optional inbound caller allow-list (`INBOUND_ALLOW` or `set_inbound` `allow`). Empty means any caller.
- When the cap is full the API returns `429` with `Retry-After`. It does not queue in another service.
- Max call length (`MAX_CALL_SECONDS`, default 600).
- A per-IP rate limit on the HTTP server.
- Logs pass through a redaction filter. Do not log the env file.

`delete_account` (MCP, `confirm: true`) removes stored secrets and call records and stops using the env-file credentials until you link them again through the tools.

## Install on a small VPS

`deploy/stackscript.sh` is a Linode StackScript (Nanode-sized). It installs baresip, PulseAudio, Python, uv, and Caddy, creates a `ringabot` user, writes the `0600` env file, generates `API_TOKEN` and the master key, enables unattended-upgrades, and opens ufw for 22, 80, and 443 only.

A release tarball download requires `RINGABOT_SHA256`. Otherwise the script clones this git repo. `deploy/cloud-init.yaml` is the same shape for other clouds. `deploy/ringabot.service` sets `NoNewPrivileges`, `ProtectSystem=strict`, and `MemoryMax=800M`.

`ringabot doctor` is the check after boot: SIP REGISTER, the xAI key, TLS, and inbound answering. See [First test call](#first-test-call).

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
