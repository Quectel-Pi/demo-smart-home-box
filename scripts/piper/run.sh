#!/bin/bash
mkdir -p /opt/piper/piper-data && cd /opt/piper
wget -O /opt/piper/piper-data/zh_CN-huayan-medium.onnx https://hf-mirror.com/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx
wget -O /opt/piper/piper-data/zh_CN-huayan-medium.onnx.json https://hf-mirror.com/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json
docker compose up -d#run piper
ufw allow 10200/tcp