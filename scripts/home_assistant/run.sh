#!/bin/bash
mkdir -p /opt/homeassistant && cd /opt/homeassistant
docker compose up -d#run homeassistant
docker ps -a#check status
apt install ufw
ufw allow 8123/tcp
# install hacs
docker exec -it homeassistant bash
wget -O - https://get.hacs.vip | bash -
exit
docker restart homeassistant