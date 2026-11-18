#!/usr/bin/env python3
"""
RKNN 推理
models下两个模型，输入都是 [1,3,640,640] float32：

    face_detection_yunet_2023mar.rknn   YuNet，12 个输出 (cls/obj/bbox/kps × stride)
    fire_smoke.rknn                     YOLOv8，output0 [1,6,8400] = 4 box + 2 类

统一接口：
    det = create_detector('face')       # 或 'fire_smoke'
    boxes = det.infer(bgr)              # [(x1, y1, x2, y2, 'face', 0.92), ...]
                                        # 坐标已还原到原图尺度
"""

import os

import cv2
import numpy as np

from rknn_rt import RKNN

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models')
INPUT_SIZE = (640, 640)


# ------------------------------------------------------------------ 小工具
def letterbox(bgr, size=INPUT_SIZE, color=114):
    """等比缩放 + 灰边填充，返回 (img, ratio, pad_x, pad_y)。"""
    h, w = bgr.shape[:2]
    dst_w, dst_h = size

    ratio = min(dst_w / w, dst_h / h)
    new_w, new_h = int(round(w * ratio)), int(round(h * ratio))

    img = np.full((dst_h, dst_w, 3), color, dtype=np.uint8)
    pad_x, pad_y = (dst_w - new_w) // 2, (dst_h - new_h) // 2
    img[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = cv2.resize(
        bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    return img, ratio, pad_x, pad_y


def restore(boxes, geo, w, h):
    """
    把输入尺寸下的坐标映射回原图并裁剪。

    geo = (scale_x, scale_y, pad_x, pad_y)。
    box 第 7 个元素（可选）是关键点列表 [(x, y), ...]，会一起映射。
    """
    sx, sy, pad_x, pad_y = geo

    def mp(x, y):
        """映射单点并裁剪到画面内"""
        return (min(max((x - pad_x) / sx, 0), w - 1),
                min(max((y - pad_y) / sy, 0), h - 1))

    out = []
    for b in boxes:
        x1, y1 = mp(b[0], b[1])
        x2, y2 = mp(b[2], b[3])
        if x2 <= x1 or y2 <= y1:
            continue
        item = (x1, y1, x2, y2, b[4], b[5])
        if len(b) > 6 and b[6]:
            item += ([mp(px, py) for px, py in b[6]],)
        out.append(item)
    return out


def nms(boxes, scores, iou_thres):
    """返回保留的下标。"""
    boxes = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
    scores = np.asarray(scores, dtype=np.float32).ravel()
    if boxes.shape[0] == 0:
        return []

    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = scores.argsort()[::-1]

    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_thres]
    return keep


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


# ------------------------------------------------------------------ 基类
class Detector:
    """
    模型加载 + 预处理 + 推理，子类只实现 postprocess。

    keep_ratio : True 用 letterbox（等比 + 灰边），False 直接拉伸。

    注意：不要在这里除 255。RKNN 转换时会把 config 里的 mean/std
    bake 进模型，rknn_inputs_set 时运行时自己执行 (x-mean)/std。
    再除一次就变成 x/65025，输入全黑，分数永远是背景值。
    颜色顺序同理：convert.py 没指定 reorder_channel 时模型要 RGB。
    """

    keep_ratio = True

    def __init__(self, model_path, conf=0.5, iou=0.45):
        self.rknn = RKNN(model_path)
        self.conf = conf
        self.iou = iou
        self._first = True

    def preprocess(self, bgr):
        """
        缩放到 640x640 -> RGB -> float32，返回 (HWC 数组, geo)。

        batch 维和 CHW 转置交给 rknn_rt.infer() 按模型 INPUT_ATTR 处理。
        """
        h, w = bgr.shape[:2]
        dst_w, dst_h = INPUT_SIZE

        if self.keep_ratio:
            img, ratio, pad_x, pad_y = letterbox(bgr)
            geo = (ratio, ratio, pad_x, pad_y)
        else:
            img = cv2.resize(bgr, (dst_w, dst_h),
                             interpolation=cv2.INTER_LINEAR)
            geo = (dst_w / w, dst_h / h, 0, 0)

        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32)
        return img, geo

    # 单帧最多保留的框数：过曝/全糊的帧上模型会乱报上百个框，
    # 全画出来就是"满屏绿"，超过上限宁可这帧不画。
    max_boxes = 20

    def infer(self, bgr):
        """BGR ndarray -> [(x1, y1, x2, y2, label, score, kps), ...]（原图坐标）"""
        if bgr is None or bgr.size == 0:
            return []

        tensor, geo = self.preprocess(bgr)
        outputs = self.rknn.infer(tensor)
        if not outputs:
            return []

        boxes = self.postprocess(outputs)
        if not boxes:
            return []

        h, w = bgr.shape[:2]
        boxes = restore(boxes, geo, w, h)

        if len(boxes) > self.max_boxes:
            return []

        if self._first:
            print(f'[detector] 检出 {len(boxes)} 个框')
            self._first = False
        return boxes

    def postprocess(self, outputs):
        raise NotImplementedError

    def release(self):
        self.rknn.release()


# ------------------------------------------------------------------ YuNet 人脸
class YunetDetector(Detector):
    """
    YuNet（与 OpenCV FaceDetectorYN 同结构）。

    输出 12 个 tensor：cls/obj/bbox/kps × stride(8/16/32)
        cx = (col + bbox[0]) * stride,  w = exp(bbox[2]) * stride
        score = sqrt(cls * obj)           kps 每点 (dx, dy)，同样乘 stride

    ⚠ cls/obj 通常已含 sigmoid，不能再套一层，否则背景 anchor 的 0.05
      会变成 0.51 全部越过阈值，每个 anchor 都画一个框 —— 满屏绿。
      这里按值域自动判断。
    """

    STRIDES = (8, 16, 32)

    def postprocess(self, outputs):
        """
        按输出名字归类，不依赖列表位置（rknn 返回顺序未必等于模型 idx 顺序）。
        """
        names = getattr(self.rknn, 'out_names', None) or []

        def collect(prefix):
            """收集某前缀的输出，返回 {stride: [N, C] ndarray}"""
            found = {}
            for i, out in enumerate(outputs):
                name = names[i] if i < len(names) else ''
                if not name.lower().startswith(prefix):
                    continue
                try:
                    stride = int(name.rsplit('_', 1)[-1])    # cls_8 -> 8
                except ValueError:
                    continue
                arr = np.asarray(out)
                found[stride] = arr[0] if arr.ndim == 3 else arr
            return found

        cls_map = collect('cls')
        obj_map = collect('obj')
        bbox_map = collect('bbox')
        kps_map = collect('kps')

        # cls/obj 通常已含 sigmoid，值域明显超出 1 才需要激活
        peak = max([float(np.max(a)) for a in
                    list(cls_map.values()) + list(obj_map.values()) if a.size]
                   or [0.0])
        need_sigmoid = peak > 1.0

        results = []
        for stride in self.STRIDES:
            if stride not in cls_map or stride not in bbox_map:
                continue

            cls = cls_map[stride].reshape(-1)
            obj = obj_map.get(stride)
            obj = obj.reshape(-1) if obj is not None else np.ones_like(cls)
            bbox = bbox_map[stride].reshape(-1, 4)

            if need_sigmoid:
                cls, obj = sigmoid(cls), sigmoid(obj)

            score = np.sqrt(np.maximum(cls, 0) * np.maximum(obj, 0))
            idx = np.where(score >= self.conf)[0]
            if idx.size == 0:
                continue

            feat = INPUT_SIZE[0] // stride
            cols = idx % feat
            rows = idx // feat

            cx = (cols + bbox[idx, 0]) * stride
            cy = (rows + bbox[idx, 1]) * stride
            w = np.exp(np.clip(bbox[idx, 2], -10, 10)) * stride
            h = np.exp(np.clip(bbox[idx, 3], -10, 10)) * stride

            kps = kps_map.get(stride)
            for n, k in enumerate(idx):
                pts = None
                if kps is not None:
                    d = kps[k]
                    pts = [((cols[n] + d[2 * j]) * stride,
                            (rows[n] + d[2 * j + 1]) * stride) for j in range(5)]
                results.append((cx[n] - w[n] / 2, cy[n] - h[n] / 2,
                                cx[n] + w[n] / 2, cy[n] + h[n] / 2,
                                'face', float(score[k]), pts))

        if not results:
            return []

        keep = nms([r[:4] for r in results], [r[5] for r in results], self.iou)
        return [results[i] for i in keep]

class FireSmokeDetector(Detector):
    """
    fire_smoke.rknn —— YOLOv8 结构，无 objectness。
    输出 output0 [1, 6, 8400] -> [8400, 6] = cx,cy,w,h, smoke, fire
    """

    # 拉伸而非 letterbox：与 ONNX 导出时的预处理保持一致
    # （blobFromImage(..., crop=False) 是直接 resize，无填充）。
    keep_ratio = False
    CLASSES = ('smoke', 'fire')

    def postprocess(self, outputs):
        pred = np.asarray(outputs[0])
        if pred.ndim == 3:
            pred = pred[0]
        if pred.shape[0] < pred.shape[1]:      # [6,8400] -> [8400,6]
            pred = pred.T

        cls_scores = pred[:, 4:]
        if cls_scores.max() > 1.0:             # 导出时没带 sigmoid 的兜底
            cls_scores = sigmoid(cls_scores)

        class_ids = cls_scores[:, :len(self.CLASSES)].argmax(axis=1)
        scores = cls_scores[np.arange(len(pred)), class_ids]

        idx = np.where(scores >= self.conf)[0]
        if idx.size == 0:
            return []

        cx, cy, w, h = pred[idx, 0], pred[idx, 1], pred[idx, 2], pred[idx, 3]

        # 坐标尺度自适应：有的导出是 0~640 像素，有的是 0~1 归一化
        if float(np.max(pred[:, :4])) <= 2.0:
            cx, cy, w, h = cx * 640, cy * 640, w * 640, h * 640

        # 宽高必须是有限正数：背景 anchor 的回归值可能是 inf/nan
        ok = np.isfinite(w) & np.isfinite(h) & (w > 1) & (h > 1)
        if not ok.all():
            idx, cx, cy, w, h = idx[ok], cx[ok], cy[ok], w[ok], h[ok]
            if idx.size == 0:
                return []

        xyxy = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)

        # 按类别分别 NMS，避免 fire/smoke 互相抑制
        results = []
        for cid in np.unique(class_ids[idx]):
            sel = np.where(class_ids[idx] == cid)[0]
            keep = nms(xyxy[sel], scores[idx][sel], self.iou)
            label = self.CLASSES[cid]
            for k in keep:
                j = sel[k]
                results.append((*xyxy[j].tolist(), label, float(scores[idx][j])))
        return results


# ------------------------------------------------------------------ 工厂
_DETECTORS = {}


def create_detector(name, conf=0.5, iou=0.45):
    """创建并缓存检测器（同名同参数只初始化一次）。"""
    name = name.lower().replace('-', '_')
    key = (name, conf, iou)
    if key in _DETECTORS:
        return _DETECTORS[key]

    if name in ('face'):
        det = YunetDetector(os.path.join(
            MODEL_DIR, 'face_detection_yunet_2023mar.rknn'), conf, iou)
    elif name in ('fire_smoke', 'fire', 'smoke'):
        det = FireSmokeDetector(os.path.join(
            MODEL_DIR, 'fire_smoke.rknn'), conf, iou)
    else:
        raise ValueError(f'未知模型: {name}（可选 face / fire_smoke）')

    _DETECTORS[key] = det
    return det


if __name__ == '__main__':
    # 离线验证：python3 detector.py <图片> [face|fire_smoke] [选项]
    import argparse

    from frame_source import draw

    p = argparse.ArgumentParser(description='用图片离线验证 RKNN 推理')
    p.add_argument('image', help='图片路径')
    p.add_argument('model', nargs='?', default='face',
                   help='face（人脸）或 fire_smoke（烟火）')
    p.add_argument('--conf', type=float, default=0.5, help='置信度阈值')
    p.add_argument('--iou', type=float, default=0.45, help='NMS 阈值')
    args = p.parse_args()

    img = cv2.imread(args.image)
    if img is None:
        raise SystemExit(f'读不到图片: {args.image}')

    det = create_detector(args.model, conf=args.conf, iou=args.iou)
    boxes = det.infer(img)
    print(f'conf={args.conf} iou={args.iou} -> 检测到 {len(boxes)} 个目标')
    for b in boxes:
        print('  (%.0f,%.0f)-(%.0f,%.0f) %s %.2f' % b[:6])

    cv2.imwrite('detect_result.jpg', draw(img, boxes))
    print('结果已保存到 detect_result.jpg')
