#!/usr/bin/env python3
"""
检测结果 -> HA binary_sensor 上报

    pip install ha-mqtt-discoverable

只在状态变化时发布，避免 20fps 下把 MQTT 刷爆；
最后一次检出后保持 hold 秒才回落 OFF，防止检测抖动导致闪烁。

⚠ name 只允许 [a-zA-Z0-9_-]：库拿它拼 MQTT topic，中文会被替换成 '-'，
  三个实体就会撞到同一个 topic 上互相覆盖。中文一律放 display_name。
"""

import time
from ha_mqtt_discoverable import Settings
from ha_mqtt_discoverable.sensors import BinarySensor, BinarySensorInfo

# detector 输出的 label -> (object_id, 中文名, device_class)
ENTITIES = {
    'face':  ('face_detected',  '人脸检测', 'motion'),
    'fire':  ('fire_detected',  '火焰检测', 'safety'),
    'smoke': ('smoke_detected', '烟雾检测', 'smoke'),
}


class Reporter:
    def __init__(self, host='127.0.0.1', port=1883, user=None, password=None,
                 node_id='m2_camera', hold=10.0, confirm=0.3):
        self.hold = hold
        self.confirm = confirm      # 秒，不是帧数
        self.sensors = {}
        self.last_seen = {}
        self.since = {}             # 本轮连续检出的起始时间
        self.on = {}

        mqtt = Settings.MQTT(host=host, port=port,
                             username=user, password=password)
        for label, (obj_id, name, dev_class) in ENTITIES.items():
            try:
                info = BinarySensorInfo(
                    name=obj_id,                    # 拼 topic，必须 ASCII
                    display_name=name,              # HA 界面显示，可中文
                    device_class=dev_class,
                    unique_id=f'{node_id}_{obj_id}',
                    object_id=obj_id,
                )
                self.sensors[label] = BinarySensor(
                    Settings(mqtt=mqtt, entity=info))
            except Exception as e:
                # 单个实体失败（如 HA 版本不认某个 device_class）不该拖垮其余
                print(f'[report] 创建 {label} 实体失败，跳过: {e}')
                continue

            self.last_seen[label] = float('-inf')   # 别用 0.0，会误报 ON
            self.since[label] = None
            self.on[label] = False
        print(f'[report] HA 实体已就绪: {", ".join(self.sensors)}')

    def update(self, labels=()):
        """
        labels: 本轮检出的 label 集合，空表示没检出。

        ON  需要「连续确认」持续 confirm 秒：
            模型偶尔对单帧误检，只凭一帧就告警的话，用户看不到框
            却会在 hold 周期内反复触发。按时间而不是按帧数，
            这样改了 --fps 语义也不会漂移。
        OFF 需要连续 hold 秒都没检出：
            避免头部转动/短暂遮挡导致告警闪烁。
        """
        now = time.monotonic()
        present = set(labels or ())

        for label, sensor in self.sensors.items():
            if label in present:
                if self.since[label] is None:
                    self.since[label] = now     # 连续检出的起点
                self.last_seen[label] = now
            else:
                self.since[label] = None

            if self.on[label]:
                on = now - self.last_seen[label] < self.hold
            else:
                on = (self.since[label] is not None and
                      now - self.since[label] >= self.confirm)

            if on == self.on[label]:            # 只在变化时发
                continue
            self.on[label] = on
            try:
                (sensor.on if on else sensor.off)()
                print(f'[report] {label} -> {"ON" if on else "OFF"}')
            except Exception as e:
                print(f'[report] {label} 发布失败: {e}')

    def close(self):
        for label, on in self.on.items():
            if on and label in self.sensors:
                try:
                    self.sensors[label].off()
                except Exception:
                    pass
