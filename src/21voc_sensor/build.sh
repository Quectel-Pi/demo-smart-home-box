#!/bin/bash
set -e

if ! command -v gcc >/dev/null 2>&1; then
    echo "missing gcc please install: apt install -y gcc" >&2
    exit 1
fi
if [ ! -f /usr/include/mosquitto.h ]; then
    echo "missing libmosquitto-dev please install: apt install -y libmosquitto-dev" >&2
    exit 1
fi

cd "$(dirname "$0")"
gcc -Wall -O2 -o 21voc 21voc.c ini.c -lmosquitto
echo "compile done: $(pwd)/21voc"
