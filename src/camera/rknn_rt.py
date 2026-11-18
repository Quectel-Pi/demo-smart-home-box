#!/usr/bin/env python3
"""
librknnrt.so 的 ctypes 封装（RK3576，运行时 2.3.2）

只实现推理必需的最小 API，替代 pip 包 rknn-toolkit-lite2：

    rknn_init / rknn_query / rknn_inputs_set / rknn_run
    rknn_outputs_get / rknn_outputs_release / rknn_destroy

用法：

    from rknn_rt import RKNN

    net = RKNN('models/xxx.rknn')
    outputs = net.infer(tensor)      # tensor: float32 NHWC, [1,H,W,3]
                                     # outputs: list[np.ndarray] float32
    net.release()

输入统一用 float32 + NHWC + pass_through=0，由运行时负责转成模型需要的
NCHW/float16，和 rknnlite 的行为一致。

⚠ 模型与 librknnrt.so 的主次版本必须匹配（当前都是 2.3.2），
  不匹配时 rknn_init 会返回 RKNN_ERR_MODEL_INVALID 或直接报 version 错误。
"""

import ctypes
import os

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
LIB_PATH = os.path.join(_HERE, 'libs', 'librknnrt.so')

# ------------------------------------------------------------------ 常量
RKNN_MAX_DIMS = 16
RKNN_MAX_NAME_LEN = 256

# rknn_query_cmd
RKNN_QUERY_IN_OUT_NUM = 0
RKNN_QUERY_INPUT_ATTR = 1
RKNN_QUERY_OUTPUT_ATTR = 2
RKNN_QUERY_SDK_VERSION = 5

# rknn_tensor_type（只保留用到的）
RKNN_TENSOR_FLOAT32 = 0

# rknn_tensor_format
RKNN_TENSOR_NCHW = 0
RKNN_TENSOR_NHWC = 1

_ERRORS = {
    0: 'RKNN_SUCC',
    -1: 'RKNN_ERR_FAIL',
    -2: 'RKNN_ERR_TIMEOUT',
    -3: 'RKNN_ERR_DEVICE_UNAVAILABLE',
    -4: 'RKNN_ERR_MALLOC_FAIL',
    -5: 'RKNN_ERR_PARAM_INVALID',
    -6: 'RKNN_ERR_MODEL_INVALID',
    -7: 'RKNN_ERR_CTX_INVALID',
    -8: 'RKNN_ERR_INPUT_INVALID',
    -9: 'RKNN_ERR_OUTPUT_INVALID',
    -10: 'RKNN_ERR_DEVICE_UNMATCH',
    -11: 'RKNN_ERR_INCOMPATILE_PRE_COMPILE_MODEL',
    -12: 'RKNN_ERR_INCOMPATILE_OPTIMIZATION_LEVEL_VERSION',
    -13: 'RKNN_ERR_TARGET_PLATFORM_UNMATCH',
}


# ------------------------------------------------------------------ 结构体
class rknn_input(ctypes.Structure):
    _fields_ = [
        ('index', ctypes.c_uint32),
        ('buf', ctypes.c_void_p),
        ('size', ctypes.c_uint32),
        ('pass_through', ctypes.c_uint8),
        ('type', ctypes.c_int),
        ('fmt', ctypes.c_int),
    ]


class rknn_output(ctypes.Structure):
    _fields_ = [
        ('want_float', ctypes.c_uint8),
        ('is_prealloc', ctypes.c_uint8),
        ('index', ctypes.c_uint32),
        ('buf', ctypes.c_void_p),
        ('size', ctypes.c_uint32),
    ]


class rknn_tensor_attr(ctypes.Structure):
    """
    严格对应 rknn_api.h 的 _rknn_tensor_attr（aarch64 下 sizeof = 376）。

    字段顺序和类型必须一字不差：rknn_query 会校验传入的 size，
    布局错位会导致读到的 shape/type 是垃圾值，且不会报错。
    """
    _fields_ = [
        ('index', ctypes.c_uint32),                   # 0
        ('n_dims', ctypes.c_uint32),                  # 4
        ('dims', ctypes.c_uint32 * RKNN_MAX_DIMS),    # 8
        ('name', ctypes.c_char * RKNN_MAX_NAME_LEN),  # 72
        ('n_elems', ctypes.c_uint32),                 # 328
        ('size', ctypes.c_uint32),                    # 332
        ('fmt', ctypes.c_int),                        # 336
        ('type', ctypes.c_int),                       # 340
        ('qnt_type', ctypes.c_int),                   # 344
        ('fl', ctypes.c_int8),                        # 348
        ('zp', ctypes.c_int32),                       # 352
        ('scale', ctypes.c_float),                    # 356
        ('w_stride', ctypes.c_uint32),                # 360
        ('size_with_stride', ctypes.c_uint32),        # 364
        ('pass_through', ctypes.c_uint8),             # 368
        ('h_stride', ctypes.c_uint32),                # 372
    ]


class rknn_input_output_num(ctypes.Structure):
    _fields_ = [('n_input', ctypes.c_uint32), ('n_output', ctypes.c_uint32)]


class rknn_sdk_version(ctypes.Structure):
    _fields_ = [
        ('api_version', ctypes.c_char * 256),
        ('drv_version', ctypes.c_char * 256),
    ]


# ------------------------------------------------------------------ 加载库
def _load_lib():
    if not os.path.exists(LIB_PATH):
        raise FileNotFoundError(
            f'找不到 {LIB_PATH}\n'
            f'请把板子上的 /usr/lib/librknnrt.so 拷到 {os.path.dirname(LIB_PATH)}/')

    try:
        lib = ctypes.CDLL(LIB_PATH)
    except OSError as e:
        raise OSError(
            f'加载 {LIB_PATH} 失败: {e}\n'
            f'注意：这个 .so 是 aarch64 的，只能在板子上跑，'
            f'WSL/PC 上无法 import。') from e

    lib.rknn_init.argtypes = [ctypes.POINTER(rknn_context), ctypes.c_void_p,
                              ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    lib.rknn_init.restype = ctypes.c_int

    lib.rknn_query.argtypes = [rknn_context, ctypes.c_int,
                               ctypes.c_void_p, ctypes.c_uint32]
    lib.rknn_query.restype = ctypes.c_int

    lib.rknn_inputs_set.argtypes = [rknn_context, ctypes.c_uint32,
                                    ctypes.POINTER(rknn_input)]
    lib.rknn_inputs_set.restype = ctypes.c_int

    lib.rknn_run.argtypes = [rknn_context, ctypes.c_void_p]
    lib.rknn_run.restype = ctypes.c_int

    lib.rknn_outputs_get.argtypes = [rknn_context, ctypes.c_uint32,
                                     ctypes.POINTER(rknn_output),
                                     ctypes.c_void_p]
    lib.rknn_outputs_get.restype = ctypes.c_int

    lib.rknn_outputs_release.argtypes = [rknn_context, ctypes.c_uint32,
                                         ctypes.POINTER(rknn_output)]
    lib.rknn_outputs_release.restype = ctypes.c_int

    lib.rknn_destroy.argtypes = [rknn_context]
    lib.rknn_destroy.restype = ctypes.c_int

    # 可选：多核并行时用（RK3576 有 3 个 core）
    if hasattr(lib, 'rknn_set_core_mask'):
        lib.rknn_set_core_mask.argtypes = [rknn_context, ctypes.c_int]
        lib.rknn_set_core_mask.restype = ctypes.c_int

    return lib


_lib = None


def get_lib():
    global _lib
    if _lib is None:
        _lib = _load_lib()
    return _lib


def _check(ret, what):
    if ret != 0:
        raise RuntimeError(f'{what} 失败: {_ERRORS.get(ret, f"code={ret}")}')


# ------------------------------------------------------------------ 主类
class RKNN:
    """一个模型对应一个 RKNN 实例。"""

    def __init__(self, model_path):
        if not os.path.exists(model_path):
            raise FileNotFoundError(f'模型不存在: {model_path}')

        lib = get_lib()

        with open(model_path, 'rb') as f:
            model = f.read()
        # 必须保持引用：rknn_init 之后 runtime 仍需访问这段内存。
        # create_string_buffer 本身就会复制一份，所以不再额外保存 model，
        # 否则几十 MB 的模型会占两份内存。
        self._model_buf = ctypes.create_string_buffer(model, len(model))

        self._ctx = rknn_context(0)
        _check(lib.rknn_init(ctypes.byref(self._ctx), self._model_buf,
                             len(model), 0, None),
               f'rknn_init({os.path.basename(model_path)})')

        # 输入输出数量
        num = rknn_input_output_num()
        _check(lib.rknn_query(self._ctx, RKNN_QUERY_IN_OUT_NUM,
                              ctypes.byref(num), ctypes.sizeof(num)),
               'rknn_query(IN_OUT_NUM)')
        self.n_input = num.n_input
        self.n_output = num.n_output

        # 输入属性（决定我们该按 NHWC 还是 NCHW 送数据）
        in_attr = rknn_tensor_attr()
        in_attr.index = 0
        _check(lib.rknn_query(self._ctx, RKNN_QUERY_INPUT_ATTR,
                              ctypes.byref(in_attr), ctypes.sizeof(in_attr)),
               'rknn_query(INPUT_ATTR)')
        self.in_fmt = in_attr.fmt
        self.in_type = in_attr.type
        self.in_dims = tuple(int(d) for d in in_attr.dims[:in_attr.n_dims])
        self.in_name = in_attr.name.decode(errors='ignore')

        # 输出形状（用于把裸 buffer reshape 成 ndarray）与名字
        self.out_shapes = []
        self.out_names = []
        for i in range(self.n_output):
            attr = rknn_tensor_attr()
            attr.index = i
            _check(lib.rknn_query(self._ctx, RKNN_QUERY_OUTPUT_ATTR,
                                  ctypes.byref(attr), ctypes.sizeof(attr)),
                   f'rknn_query(OUTPUT_ATTR[{i}])')
            self.out_shapes.append(
                tuple(int(d) for d in attr.dims[:attr.n_dims]))
            self.out_names.append(attr.name.decode(errors='ignore'))

        self._released = False

    # ---------------------------------------------------------- 版本信息
    def version(self):
        """返回 (api_version, drv_version)。"""
        lib = get_lib()
        ver = rknn_sdk_version()
        ret = lib.rknn_query(self._ctx, RKNN_QUERY_SDK_VERSION,
                             ctypes.byref(ver), ctypes.sizeof(ver))
        if ret != 0:
            return None
        return (ver.api_version.decode(errors='ignore'),
                ver.drv_version.decode(errors='ignore'))

    # ---------------------------------------------------------- 推理
    def infer(self, tensor):
        """
        tensor: float32，布局为 HWC（[H, W, 3]）或 CHW（[3, H, W]），
                也可带 batch 维。
        返回: list[np.ndarray] （float32，形状见模型输出）

        这里根据模型 INPUT_ATTR 里声明的 layout 自动决定用什么 fmt 送数据，
        避免"模型要 NCHW 却按 NHWC 传"导致输入被静默打散。
        """
        if self._released:
            raise RuntimeError('RKNN 已 release')

        lib = get_lib()

        if tensor.dtype != np.float32:
            tensor = tensor.astype(np.float32)

        # 去掉可能存在的 batch 维，统一按 HWC / CHW 处理
        arr = tensor
        if arr.ndim == 4:
            arr = arr[0]

        want_nchw = (self.in_fmt == RKNN_TENSOR_NCHW)

        if arr.ndim == 3:
            is_chw = arr.shape[0] in (1, 3) and arr.shape[-1] not in (1, 3)
            if want_nchw and not is_chw:
                arr = arr.transpose(2, 0, 1)        # HWC -> CHW
            elif not want_nchw and is_chw:
                arr = arr.transpose(1, 2, 0)        # CHW -> HWC

        arr = np.ascontiguousarray(arr[None])       # 补回 batch 维

        fmt = RKNN_TENSOR_NCHW if want_nchw else RKNN_TENSOR_NHWC
        tensor = arr

        inputs = (rknn_input * 1)()
        inputs[0].index = 0
        inputs[0].buf = tensor.ctypes.data
        inputs[0].size = tensor.size * tensor.itemsize
        inputs[0].pass_through = 0
        inputs[0].type = RKNN_TENSOR_FLOAT32
        inputs[0].fmt = fmt

        _check(lib.rknn_inputs_set(self._ctx, 1, inputs), 'rknn_inputs_set')
        _check(lib.rknn_run(self._ctx, None), 'rknn_run')

        outputs = (rknn_output * self.n_output)()
        for i in range(self.n_output):
            outputs[i].index = i
            outputs[i].want_float = 1      # fp16 输出转 float32
            outputs[i].is_prealloc = 0

        _check(lib.rknn_outputs_get(self._ctx, self.n_output, outputs, None),
               'rknn_outputs_get')

        try:
            result = []
            for i in range(self.n_output):
                shape = self.out_shapes[i]
                n = int(np.prod(shape)) if shape else 0
                if n <= 0:
                    result.append(np.empty((0,), dtype=np.float32))
                    continue
                arr = np.ctypeslib.as_array(
                    ctypes.cast(outputs[i].buf, ctypes.POINTER(ctypes.c_float)),
                    shape=(n,)).copy()
                result.append(arr.reshape(shape))
            return result
        finally:
            lib.rknn_outputs_release(self._ctx, self.n_output, outputs)

    # ---------------------------------------------------------- 释放
    def release(self):
        if self._released:
            return
        try:
            get_lib().rknn_destroy(self._ctx)
        finally:
            self._released = True

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass


rknn_context = ctypes.c_uint64


if __name__ == '__main__':
    # 板子上自检：python3 rknn_rt.py [model.rknn]
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        _HERE, 'models', 'face_detection_yunet_2023mar.rknn')

    net = RKNN(path)
    print(f'模型: {os.path.basename(path)}')
    print(f'SDK: {net.version()}')
    print(f'输入 {net.n_input} 个, 输出 {net.n_output} 个')
    for i, s in enumerate(net.out_shapes):
        print(f'  out[{i}] {s}')

    dummy = np.zeros((1, 640, 640, 3), dtype=np.float32)
    outs = net.infer(dummy)
    print('\n推理自检：')
    for i, o in enumerate(outs):
        print(f'  out[{i}] shape={o.shape} range=[{o.min():.3f}, {o.max():.3f}]')
    net.release()
    print('OK')
