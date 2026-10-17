from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_unit_is_hardened():
    unit = (ROOT / "deploy" / "ringabot.service").read_text(encoding="utf-8")
    assert "NoNewPrivileges=true" in unit
    assert "MemoryMax=800M" in unit
    assert "User=ringabot" in unit
    assert "SupplementaryGroups=audio" in unit
    assert "snd-aloop" in unit
    assert "pulseaudio" not in unit
    assert "127.0.0.1" in (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")


def test_stackscript_keeps_secrets_local():
    script = (ROOT / "deploy" / "stackscript.sh").read_text(encoding="utf-8")
    assert "chmod 0600 /etc/ringabot/ringabot.env" in script
    assert "chmod 0600 /root/ringabot-credentials.txt" in script
    assert "sha256sum -c" in script
    assert "ufw allow 22/tcp" in script
    assert "quote_env" in script
    assert "AUDIO_MODE=alsa" in script
    assert "SIP_USERNAME=" in script
    before, after = script.split('if [[ -n "${public_hostname:-}" ]]', 1)
    assert "ufw allow 80/tcp" not in before
    assert "ufw allow 443/tcp" not in before
    body, rest = after.split("\nfi\n", 1)
    assert "ufw allow 80/tcp" in body
    assert "ufw allow 443/tcp" in body
    assert "ufw allow 80/tcp" not in rest
    assert "ufw allow 443/tcp" not in rest
    assert "ufw allow 22/tcp" in rest
    cloud = (ROOT / "deploy" / "cloud-init.yaml").read_text(encoding="utf-8")
    assert 'permissions: "0600"' in cloud
    assert "AUDIO_MODE=alsa" in cloud
    assert 'PERSONA_NAME="Example Assistant"' in cloud
    commands = [line.strip() for line in cloud.splitlines()]
    assert "- ufw allow 80/tcp" not in commands
    assert "- ufw allow 443/tcp" not in commands
    firewall = cloud.split("runcmd:", 1)[1]
    assert "PUBLIC_HOSTNAME" in firewall
    assert "ufw allow 80/tcp" in firewall
    assert "ufw allow 443/tcp" in firewall


def test_runtime_does_not_import_a_broker_or_db_server():
    banned = ("redis", "celery", "pika", "kafka", "amqp", "sqlalchemy", "psycopg", "pymongo")
    for path in (ROOT / "src" / "ringabot").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in banned:
            assert name not in text, path.name


def test_readme_covers_security_and_webhooks():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    for phrase in (
        "telemarketing",
        "robocall",
        "911",
        "ai",
        "sqlite",
        "hmac",
        "constant",
        "no secrets",
        "voip.ms",
    ):
        assert phrase in readme, phrase
