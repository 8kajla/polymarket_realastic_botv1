#!/bin/bash
# EC2 user-data (cloud-init) bootstrap for the paperbot, on a bare
# Ubuntu 24.04 LTS AMI. Installs Python + git, clones this repo, sets up
# a venv, and runs the bot under systemd with auto-restart -- systemd's
# Restart=on-failure here is the AWS-side equivalent of Railway's
# restartPolicyType in railway.toml.
#
# Persistence note: unlike Railway's ephemeral container filesystem, an
# EC2 instance's root EBS volume survives reboots/stops on its own -- no
# separate volume-attachment step needed for PAPERBOT_DATA_DIR to persist
# (just don't TERMINATE the instance; stop/start/reboot are all fine).
#
# Usage: pass this file as --user-data to `aws ec2 run-instances` (or
# paste into the EC2 console's "User data" field at launch). Idempotent
# enough to re-run by hand over SSH if you need to redeploy without a
# fresh instance (e.g. after `git pull`).
set -e
export DEBIAN_FRONTEND=noninteractive
exec > /var/log/paperbot-setup.log 2>&1

echo "=== paperbot setup starting $(date -u) ==="

apt-get update -y
apt-get install -y python3 python3-venv python3-pip git

id -u paperbot &>/dev/null || useradd --system --create-home --home-dir /opt/paperbot --shell /usr/sbin/nologin paperbot

rm -rf /opt/paperbot/app
git clone https://github.com/8kajla/polymarket_realastic_botv1.git /opt/paperbot/app
cd /opt/paperbot/app

python3 -m venv /opt/paperbot/venv
/opt/paperbot/venv/bin/pip install --upgrade pip
/opt/paperbot/venv/bin/pip install -r requirements.txt

mkdir -p /opt/paperbot/data
chown -R paperbot:paperbot /opt/paperbot

cat > /etc/systemd/system/paperbot.service << 'UNIT'
[Unit]
Description=Polymarket paper-trading bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=paperbot
Group=paperbot
WorkingDirectory=/opt/paperbot/app
Environment=PAPERBOT_DATA_DIR=/opt/paperbot/data
Environment=QUEUE_SAFETY_FACTOR=0.25
# BNB, Dogecoin, and Hyperliquid excluded deliberately: the real trader
# this bot replicates has stopped trading all three (last real trades:
# Dogecoin/Hyperliquid 2026-08-06, BNB 2026-08-23 -- a progressive
# narrowing down to Bitcoin/Ethereum/Solana only, not a single BNB
# event). See "Deliberately excluded" below for the full verification.
ExecStart=/opt/paperbot/venv/bin/python -u run_bot.py --assets Bitcoin Ethereum Solana
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable paperbot.service
systemctl start paperbot.service

echo "=== paperbot setup complete $(date -u) ==="
