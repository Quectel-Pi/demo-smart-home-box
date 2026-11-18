#!/usr/bin/env python3
"""
摄像头采集（GStreamer + appsink），输出 BGR ndarray

    from frame_source import get_stream, draw, encode_jpeg

    stream = get_stream()
    stream.start()
    seq, frame = stream.wait_after(None, timeout=5)   # 等首帧
    seq, frame = stream.wait_after(seq, timeout=1)    # 等下一帧

管线：v4l2src -> image/jpeg -> appsink -> cv2.imdecode -> BGR
"""

import os
import threading

import cv2
import numpy as np

import gi
gi.require_version('Gst', '1.0')
# GstApp 这里虽未直接引用，但 appsink 元素依赖它的 typelib 才能创建
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GstApp, GLib  # noqa: F401

Gst.init(None)

DEVICE = os.environ.get('CAMERA_DEVICE', '/dev/video73')
WIDTH = int(os.environ.get('CAMERA_WIDTH', 1280))
HEIGHT = int(os.environ.get('CAMERA_HEIGHT', 720))
FPS = int(os.environ.get('CAMERA_FPS', 30))

PIPELINE = (
    f'v4l2src device={DEVICE} '
    f'! image/jpeg,width={WIDTH},height={HEIGHT},framerate={FPS}/1 '
    f'! appsink name=mysink emit-signals=true sync=false max-buffers=2 drop=true'
)


def encode_jpeg(bgr, quality=70):
    ok, buf = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


def draw(bgr, boxes, color=(0, 255, 0), thickness=2, draw_kps=True):
    """
    画框，返回新帧（不改原帧）

    boxes: [(x1, y1, x2, y2), ...]
           [(x1, y1, x2, y2, label, score), ...]
           [(x1, y1, x2, y2, label, score, [(px, py), ...]), ...]

    最后一个元素是关键点列表（YuNet 的 5 点），draw_kps=True 时一并画出。
    """
    out = bgr.copy()
    for b in boxes:
        # 跳过非法坐标：nan/inf 转 int 会抛异常，把整个推流线程打死
        if not all(np.isfinite(v) for v in b[:4]):
            continue
        x1, y1, x2, y2 = (int(v) for v in b[:4])
        if x2 <= x1 or y2 <= y1:
            continue
        cv2.rectangle(out, (x1, y1), (x2, y2), color, thickness)

        if len(b) >= 6:
            text = f'{b[4]} {b[5]:.2f}'
            (tw, th), base = cv2.getTextSize(
                text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
            # 标签框别溢出画面顶部
            ty = max(y1, th + base + 4)
            cv2.rectangle(out, (x1, ty - th - base - 4),
                          (x1 + tw + 4, ty), color, -1)
            cv2.putText(out, text, (x1 + 2, ty - base - 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1, cv2.LINE_AA)

        # 关键点：5 个圆点（眼睛/鼻尖/嘴角）
        if draw_kps and len(b) > 6 and b[6]:
            for px, py in b[6]:
                cv2.circle(out, (int(px), int(py)), 3, (0, 0, 255), -1)
    return out


class CameraStream:
    def __init__(self, pipeline=PIPELINE):
        self._pipeline_str = pipeline
        self._cond = threading.Condition()
        self._frame = None
        self._seq = 0
        self._pipeline = None
        self._loop = None
        self._glib_thread = None

    def start(self):
        if self._pipeline is not None:
            return self

        try:
            self._pipeline = Gst.parse_launch(self._pipeline_str)
        except GLib.Error as e:
            raise RuntimeError(f'pipeline 解析失败: {e.message}') from e

        sink = self._pipeline.get_by_name('mysink')
        sink.connect('new-sample', self._on_sample)

        # GLib 主循环跑在后台线程，主线程留给 Flask / 推理
        self._loop = GLib.MainLoop()
        self._glib_thread = threading.Thread(target=self._loop.run,
                                             name='camera-glib', daemon=True)
        self._glib_thread.start()

        if self._pipeline.set_state(Gst.State.PLAYING) == \
                Gst.StateChangeReturn.FAILURE:
            self.stop()
            raise RuntimeError(f'pipeline 启动失败（检查 {DEVICE} 的格式与权限）')
        return self

    def stop(self):
        if self._loop is not None:
            self._loop.quit()
        if self._glib_thread is not None:
            # 等 GLib 循环真正退出，否则重启时会短暂有两个 MainLoop
            self._glib_thread.join(timeout=2.0)
            self._glib_thread = None
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
        self._loop = None
        with self._cond:
            self._frame = None
            self._cond.notify_all()

    def _on_sample(self, sink):
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.OK

        buf = sample.get_buffer()
        structure = sample.get_caps().get_structure(0)

        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            frame = cv2.imdecode(
                np.frombuffer(info.data, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                return Gst.FlowReturn.OK
        finally:
            buf.unmap(info)

        with self._cond:
            self._frame = frame
            self._seq += 1
            self._cond.notify_all()
        return Gst.FlowReturn.OK

    def wait_after(self, last_seq, timeout=1.0):
        """等序号大于 last_seq 的帧，返回 (seq, frame)；超时 (last_seq, None)。"""
        with self._cond:
            if not self._cond.wait_for(
                    lambda: self._frame is not None and
                    (last_seq is None or self._seq > last_seq), timeout):
                return last_seq, None
            return self._seq, self._frame.copy()

    def first_frame(self, timeout=5.0):
        """等首帧，超时抛异常并给出排查命令。"""
        seq, frame = self.wait_after(None, timeout)
        if frame is None:
            raise RuntimeError(
                f'{timeout:.0f}s 内没有帧，排查：\n'
                f'  v4l2-ctl -d {DEVICE} --list-formats-ext\n'
                f'  gst-launch-1.0 v4l2src device={DEVICE} ! fakesink -v')
        return seq, frame

_stream = None
_lock = threading.Lock()
def get_stream():
    global _stream
    with _lock:
        if _stream is None:
            _stream = CameraStream()
        return _stream
