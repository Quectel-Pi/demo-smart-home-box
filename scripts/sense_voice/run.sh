#!/bin/bash
mkdir /opt/sensevoice && cd /opt/sensevoice
docker compose up -d#run sensevoice
ufw allow 10300/tcp