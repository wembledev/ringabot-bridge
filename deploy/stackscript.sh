#!/bin/bash
# Linode StackScript for a single self-hosted ringabot-bridge.
# UDF values are written only to /etc/ringabot/ringabot.env (mode 0600).
# They are never printed. Linode keeps UDF input in metadata; rotate the
# SIP password after setup if that bothers you.
#
# <UDF name="xai_api_key" label="xAI API key" />
# <UDF name="voipms_sip_server" label="Voip.ms SIP server" example="your-pop.example.com" />
# <UDF name="voipms_sip_user" label="Voip.ms sub-account username (not the DID)" />
# <UDF name="voipms_sip_password" label="Voip.ms SIP password" />
# <UDF name="voipms_did" label="Voip.ms caller ID (E.164)" example="+15555550100" />
# <UDF name="persona_name" label="Persona name" example="Example Assistant" default="Example Assistant" />
# <UDF name="persona_instructions" label="Persona instructions" default="Be brief and polite." />
# <UDF name="public_hostname" label="Public hostname for TLS (optional)" example="phone.example.com" default="" />
# <UDF name="tools_webhook_url" label="Tools webhook URL (optional)" default="" />
# <UDF name="tools_webhook_secret" label="Tools webhook HMAC secret (optional)" default="" />
# <UDF name="allowed_destinations" label="Allowed destination prefixes" default="+1" example="+1" />

set -euo pipefail
umask 077

export DEBIAN_FRONTEND=noninteractive

require() {
  local name="$1"
  local value="$2"
  if [[ -z "${value}" ]]; then
    echo "missing UDF field ${name}" >&2
    exit 1
  fi
  if [[ "${value}" == *$'\n'* ]]; then
    echo "refusing multiline UDF field ${name}" >&2
    exit 1
  fi
}

require xai_api_key "${xai_api_key:-}"
require voipms_sip_server "${voipms_sip_server:-}"
require voipms_sip_user "${voipms_sip_user:-}"
require voipms_sip_password "${voipms_sip_password:-}"
require voipms_did "${voipms_did:-}"

apt-get update
apt-get install -y --no-install-recommends \
  baresip \
  alsa-utils \
  python3 \
  ca-certificates \
  curl \
  git \
  openssl \
  ufw \
  unattended-upgrades \
  caddy

if ! id ringabot >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/ringabot --shell /usr/sbin/nologin ringabot
fi
usermod -aG audio ringabot
echo snd-aloop > /etc/modules-load.d/snd-aloop.conf
modprobe snd-aloop || true
install -d -o ringabot -g ringabot -m 0750 /var/lib/ringabot /opt/ringabot /etc/ringabot

if [[ ! -x /usr/local/bin/uv ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  install -m 0755 "${HOME}/.local/bin/uv" /usr/local/bin/uv
fi

if [[ -n "${RINGABOT_TARBALL_URL:-}" ]]; then
  if [[ -z "${RINGABOT_SHA256:-}" ]]; then
    echo "RINGABOT_SHA256 is required with RINGABOT_TARBALL_URL" >&2
    exit 1
  fi
  curl -fsSL "${RINGABOT_TARBALL_URL}" -o /tmp/ringabot.tar.gz
  echo "${RINGABOT_SHA256}  /tmp/ringabot.tar.gz" | sha256sum -c -
  tar -xzf /tmp/ringabot.tar.gz -C /opt/ringabot --strip-components=1
  rm -f /tmp/ringabot.tar.gz
else
  rm -rf /opt/ringabot
  git clone --depth 1 --branch "${RINGABOT_REF:-main}" \
    "${RINGABOT_GIT_URL:-https://github.com/wembledev/ringabot-bridge.git}" \
    /opt/ringabot
  if [[ -n "${RINGABOT_GIT_SHA:-}" ]]; then
    actual="$(git -C /opt/ringabot rev-parse HEAD)"
    if [[ "${actual}" != "${RINGABOT_GIT_SHA}" ]]; then
      echo "git sha mismatch" >&2
      exit 1
    fi
  fi
fi
chown -R ringabot:ringabot /opt/ringabot
sudo -u ringabot -H uv sync --frozen --no-dev --project /opt/ringabot

openssl rand -out /etc/ringabot/master.key 32
chown ringabot:ringabot /etc/ringabot/master.key
chmod 0400 /etc/ringabot/master.key

api_token="$(openssl rand -hex 32)"
export xai_api_key voipms_sip_server voipms_sip_user voipms_sip_password voipms_did
export persona_name="${persona_name:-Example Assistant}"
export persona_instructions="${persona_instructions:-Be brief and polite.}"
export tools_webhook_url="${tools_webhook_url:-}"
export tools_webhook_secret="${tools_webhook_secret:-}"
export public_hostname="${public_hostname:-}"
export allowed_destinations="${allowed_destinations:-+1}"
export api_token
# The project venv, not a shell heredoc, so '$' is not expanded and values
# with spaces (PERSONA_NAME) stay quoted.
/opt/ringabot/.venv/bin/python - <<'PY'
import os
from pathlib import Path

from ringabot.envfile import quote_env

def value(name: str) -> str:
    raw = os.environ.get(name, "")
    if "\n" in raw or "\r" in raw:
        raise SystemExit(f"refusing multiline value for {name}")
    return quote_env(raw)

lines = [
    f"XAI_API_KEY={value('xai_api_key')}",
    "XAI_VOICE_MODEL=grok-voice-latest",
    "XAI_VOICE=aurora",
    f"VOIPMS_SIP_SERVER={value('voipms_sip_server')}",
    f"VOIPMS_SIP_USER={value('voipms_sip_user')}",
    f"SIP_USERNAME={value('voipms_sip_user')}",
    f"VOIPMS_SIP_PASSWORD={value('voipms_sip_password')}",
    f"VOIPMS_DID={value('voipms_did')}",
    f"PERSONA_NAME={value('persona_name')}",
    f"PERSONA_INSTRUCTIONS={value('persona_instructions')}",
    "GREETING=",
    f"TOOLS_WEBHOOK_URL={value('tools_webhook_url')}",
    f"TOOLS_WEBHOOK_SECRET={value('tools_webhook_secret')}",
    f"API_TOKEN={value('api_token')}",
    f"PUBLIC_HOSTNAME={value('public_hostname')}",
    "INBOUND_MODE=off",
    "RECORDINGS=off",
    "RETENTION_DAYS=30",
    f"ALLOWED_DESTINATIONS={value('allowed_destinations')}",
    "MAX_CALLS_PER_DAY=20",
    "MAX_CONCURRENT_CALLS=4",
    "HOST=127.0.0.1",
    "PORT=8787",
    "DATA_DIR=/var/lib/ringabot",
    "AUDIO_MODE=alsa",
    "RINGABOT_MASTER_KEY_FILE=/etc/ringabot/master.key",
]
path = Path("/etc/ringabot/ringabot.env")
path.write_text("\n".join(lines) + "\n")
os.chmod(path, 0o600)
PY
chown ringabot:ringabot /etc/ringabot/ringabot.env
chmod 0600 /etc/ringabot/ringabot.env

if [[ -n "${public_hostname:-}" ]]; then
  if [[ ! "${public_hostname}" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "invalid public_hostname" >&2
    exit 1
  fi
  api_url="https://${public_hostname}/mcp"
  cat > /etc/caddy/Caddyfile <<EOF
${public_hostname} {
    reverse_proxy 127.0.0.1:8787
}
EOF
  systemctl enable caddy
  systemctl restart caddy
  ufw allow 80/tcp
  ufw allow 443/tcp
else
  api_url="http://127.0.0.1:8787/mcp"
fi

cat > /root/ringabot-credentials.txt <<EOF
API_URL=${api_url}
API_TOKEN=${api_token}
EOF
chmod 0600 /root/ringabot-credentials.txt

install -m 0644 /opt/ringabot/deploy/ringabot.service /etc/systemd/system/ringabot.service
systemctl daemon-reload
systemctl enable ringabot
systemctl restart ringabot

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

dpkg-reconfigure -f noninteractive unattended-upgrades || true

echo "credentials written to /root/ringabot-credentials.txt"
