#!/bin/bash

#pipewire needs to be run as a non-root user
useradd -m -s /bin/bash lva
passwd lva
usermod -aG audio lva
sudo loginctl enable-linger lva
su lva
#LVA depends on pipewire
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
systemctl --user status pipewire-pulse
systemctl --user start pipewire-pulse

cd /opt
git clone https://github.com/OHF-Voice/linux-voice-assistant.git
cd linux-voice-assistant

#Install dependencies
sudo apt install -y libmpv-dev
python3 -m venv --system-site-packages ./.lva_venv
source ./.lva_venv/bin/activate
pip install -e .

#select appropriate input and output devices
python3 -m linux_voice_assistant --list-input-devices
python3 -m linux_voice_assistant --list-output-devices
#run LVA service
python3 -m linux_voice_assistant --name "M2-LVA" \
--audio-input-device "<embeded mic device>" \
--audio-output-device "<output speaker device>" --port 6053