#!/bin/bash
mkdir -p /opt/mqtt/mosquitto/{config,data,log}
cd /opt/mqtt

touch /opt/mqtt/mosquitto/config/mosquitto.conf
cat <<EOL > /opt/mqtt/mosquitto/config/mosquitto.conf
listener 1883
allow_anonymous false
password_file /mosquitto/config/passwd
EOL
#set username and password for mqtt
touch /opt/mqtt/mosquitto/config/passwd
chown -R 1883:1883 /opt/mqtt/mosquitto
docker compose up -d
docker exec -it mosquitto mosquitto_passwd -b /mosquitto/config/passwd <username> <password>
docker restart mosquitto