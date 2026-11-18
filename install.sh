#!/usr/bin/env bash
#
# 家庭盒子 - 环境部署脚本
#
#   sudo ./install.sh            部署全部（21VOC 传感器 + 摄像头）
#   sudo ./install.sh sensor     只部署 21VOC 传感器
#   sudo ./install.sh camera     只部署摄像头
#
# 装完后用 ./setup_autostart.sh install 配置开机自启。
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/homebox
CONF_DIR=/etc/homebox

MOSQ_DEPS=(libmosquitto-dev gcc)
CAM_DEPS=(python3 python3-venv python3-pip
          python3-gi python3-gi-cairo
          gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
          gstreamer1.0-tools gstreamer1.0-plugins-base
          gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
          gstreamer1.0-rockchip1
          python3-opencv
          python3-numpy)

info() { echo "==> $*"; }
warn() { echo "  [WARN] $*"; }

install_conf() {
  local src="$1" dst="$2"
  install -d "$(dirname "$dst")"
  if [ -f "$dst" ]; then
      echo "  reserve previous conf  $dst"
  else
      install -m600 "$src" "$dst"
      echo "  install conf $dst"
  fi
}

install_sensor() {
  local dir="$APP_DIR/src/21voc_sensor"
  info "start deply 21VOC sensor ..."

  apt install -y "${MOSQ_DEPS[@]}"

  info "compile 21voc"
  ( cd "$dir" && ./build.sh )

  install -Dm755 "$dir/21voc" /usr/local/bin/21voc
  install_conf "$dir/21voc.conf" "$CONF_DIR/21voc.conf"
  install -Dm644 "$dir/21voc.service" /etc/systemd/system/21voc.service

  echo "deploy 21voc sensor done"
}

install_camera() {
  local dir="$APP_DIR/src/camera"
  info "start deply camera ..."

  apt install -y "${CAM_DEPS[@]}"
  install -d "$PREFIX"
  rm -rf "$PREFIX/camera"
  cp -r "$dir" "$PREFIX/camera"

  info "create camera venv $PREFIX/venv"
  rm -rf "$PREFIX/venv"
  python3 -m venv --system-site-packages "$PREFIX/venv"
  (
      . "$PREFIX/venv/bin/activate"
      pip install -q --upgrade pip
      pip install -q flask ha-mqtt-discoverable
  ) || warn "flask / ha-mqtt-discoverable install failed"

  install_conf "$dir/camera.env" "$CONF_DIR/camera.env"
  install -Dm644 "$dir/camera.service" /etc/systemd/system/camera.service

  echo "deploy camera done"
}

usage() {
  echo "usage: sudo $0 [all|sensor|camera]"
  echo "  all     deploy both sensor and camera"
  echo "  sensor  only deploy 21VOC sensor"
  echo "  camera  only deploy camera"
}

# main
TARGET="${1:-all}"

case "$TARGET" in
    all|sensor|camera) ;;
    -h|--help|help) usage; exit 0 ;;
    *)
        echo "unknown target: $TARGET" >&2
        usage >&2
        exit 1
        ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    echo "please use root to run: sudo $0 $TARGET" >&2
    exit 1
fi

if ! command -v apt >/dev/null 2>&1; then
    echo "rely on apt(Debian/Ubuntu), not support on this system" >&2
    exit 1
fi

apt update

case "$TARGET" in
    all)    install_sensor; install_camera ;;
    sensor) install_sensor ;;
    camera) install_camera ;;
esac

systemctl daemon-reload

echo ""
echo "Deployment complete."
echo "next step: ./setup_autostart.sh install    # 配置开机自启"
