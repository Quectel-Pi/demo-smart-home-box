#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import threading
import time
from flask import Flask, Response, jsonify, request
from frame_source import draw, encode_jpeg, get_stream

app = Flask(__name__)
stream = get_stream()

JPEG_QUALITY = 70
FPS = 20
detector = None
reporter = None

# 后台线程产出的最新一帧，推流和抓图都从这里取
_latest = {'seq': -1, 'jpeg': None}
_latest_lock = threading.Lock()


def detect(frame):
    """推理 -> 上报 -> 画框 -> 编码。只在 worker 单线程里调用，不需加锁。"""
    if detector is not None:
        boxes = detector.infer(frame)

        # 上报失败不能影响推流，单独隔离
        if reporter is not None:
            try:
                reporter.update({b[4] for b in boxes})
            except Exception as e:
                print(f'[report] {e}')

        frame = draw(frame, boxes)

    return encode_jpeg(frame, JPEG_QUALITY)


def worker(stop):
    """
    持续取帧 -> 推理 -> 上报 -> 编码，与有没有客户端无关。

    上报不依赖"有人看流"的关键就在这里：HTTP 请求只读结果，不做推理。
    """
    interval = 1.0 / FPS if FPS else 0
    last_seq = None
    while not stop.is_set():
        seq, frame = stream.wait_after(last_seq, timeout=1.0)
        if frame is None:
            continue
        last_seq = seq

        try:
            jpeg = detect(frame)
        except Exception as e:
            print(f'[worker] {e}')
            continue

        if jpeg is not None:
            with _latest_lock:
                _latest['seq'] = seq
                _latest['jpeg'] = jpeg

        if interval:
            time.sleep(interval)


def wait_latest(after_seq, timeout=2.0):
    """等一帧比 after_seq 更新的；after_seq 为 None 时立刻返回当前帧。"""
    deadline = time.monotonic() + timeout
    while True:
        with _latest_lock:
            seq, jpeg = _latest['seq'], _latest['jpeg']
        if jpeg is not None and (after_seq is None or seq > after_seq):
            return seq, jpeg
        if time.monotonic() >= deadline:
            return seq, None
        time.sleep(0.02)


def generate_frames():
    after = None
    while True:
        # worker 已按 FPS 节流，这里只需跟上它的产出，不再额外 sleep
        seq, jpeg = wait_latest(after, timeout=1.0)
        if jpeg is None:
            continue
        after = seq

        # Content-Length 不能省：部分浏览器解析无长度的 MJPEG 会错帧
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n'
               b'Content-Length: ' + str(len(jpeg)).encode() + b'\r\n\r\n'
               + jpeg + b'\r\n')


@app.route('/video')
def video():
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/snapshot')
def snapshot():
    _, jpeg = wait_latest(request.args.get('seq', type=int), timeout=2.0)
    if jpeg is None:
        return jsonify({'ok': False, 'msg': 'no frame'}), 503

    return Response(jpeg, mimetype='image/jpeg',
                    headers={'Cache-Control': 'no-store'})

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--model', default='', help='face / fire_smoke，留空只推流')
    p.add_argument('--conf', type=float, default=0.45, help='置信度阈值')
    p.add_argument('--iou', type=float, default=0.45, help='NMS 阈值')
    p.add_argument('--fps', type=float, default=20, help='推理/推流帧率上限')
    p.add_argument('--port', type=int, default=8080)
    p.add_argument('--no-report', action='store_true', help='关闭 HA 上报')
    p.add_argument('--mqtt-host', default='127.0.0.1')
    p.add_argument('--mqtt-port', type=int, default=1883)
    p.add_argument('--mqtt-user', default=None)
    p.add_argument('--mqtt-pass', default=None)
    p.add_argument('--node-id', default='m2_camera',
                   help='MQTT 设备标识，实体 unique_id 前缀')
    p.add_argument('--hold', type=float, default=10.0,
                   help='最后一次检出后保持 ON 的秒数')
    p.add_argument('--confirm', type=float, default=0.3,
                   help='连续检出多少秒才判定为 ON，用于压误检')
    args = p.parse_args()
    FPS = args.fps

    stream.start()
    try:
        seq, _ = stream.first_frame(timeout=5.0)
        print(f'首帧 seq={seq}')
    except RuntimeError as e:
        stream.stop()
        raise SystemExit(f'启动失败: {e}')

    if args.model:
        from detector import create_detector
        detector = create_detector(args.model, conf=args.conf, iou=args.iou)
        print(f'模型: {args.model}  conf={args.conf}  iou={args.iou}')

        if not args.no_report:      # 只有开了检测才有东西可报
            from ha_report import Reporter
            reporter = Reporter(args.mqtt_host, args.mqtt_port,
                                args.mqtt_user, args.mqtt_pass,
                                args.node_id, args.hold, args.confirm)
            print(f'告警: hold={args.hold}s  confirm={args.confirm}s')
    else:
        print('未启用推理')

    stop = threading.Event()
    t = threading.Thread(target=worker, args=(stop,), name='infer', daemon=True)
    t.start()

    print(f'http://0.0.0.0:{args.port}/video')
    try:
        app.run(host='0.0.0.0', port=args.port, threaded=True,
                use_reloader=False)
    finally:
        stop.set()
        # 必须等 worker 退出：它可能正卡在 detector.infer() 里，
        # 直接 release() 会用到已销毁的 rknn context（段错误）
        t.join(timeout=5.0)
        stream.stop()
        if reporter is not None:
            reporter.close()
        if detector is not None:
            detector.release()