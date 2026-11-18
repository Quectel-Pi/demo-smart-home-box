#!/bin/bash
v4l2-ctl --list-devices
#查看摄像头支持的帧格式
v4l2-ctl -d /dev/video73 --list-formats-ext
#查看GStreamer中关于rk的插件和元素信息
gst-inspect-1.0 | grep -i -E "mpp|rockchip|rga"
#测试采集通路,采集30张
gst-launch-1.0 -e v4l2src device=/dev/video73 num-buffers=30 ! \
  image/jpeg,width=1280,height=720,framerate=30/1 ! \
  mppjpegdec ! mppjpegenc ! \
  multifilesink location=/tmp/frame_%03d.jpg