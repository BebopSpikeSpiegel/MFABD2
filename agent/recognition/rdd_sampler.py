# ================================================================
# == RedDotDetector 样本采集器（v3 定标语料地基）                 ==
# ================================================================
# 作用：把每次识别(命中/未命中)的小图 + 完整识别信息落成"语料即回归集"：
#   · 小图：roi_crop / red_mask / inner，唯一命名(时间戳)累积，不覆盖；
#   · samples.jsonl.log：一行一事件(时间/节点/roi/box/conf 四项分解/红块几何/生效参数/关联图)。
#   事后整个文件夹拿走，即可离线回放：改任何参数先过旧语料，再上真机。
#
# 台账为什么叫 .jsonl.log(别改回去)：UI 的"导出日志"按扩展名白名单收集文件，.jsonl
#   不在名单内会被静默丢掉——2026-07 收到的四个用户回流包共 688 张图全部没有台账，
#   根因即此(图是 .png 在名单内，台账不在)。没有台账的图对回放器等于零价值：roi /
#   params / result / box 全在台账里。补一个 .log 后缀即可随包带出，内容仍是 JSONL。
#
# 开关(模式)：RDD_SAMPLE 环境变量(off/fail/all) > maa_option.json 的 rdd_sample > 默认 all。
#   env 穿透运行侧一切配置；fail=只采未命中；off=完全关闭(零开销)。
#
# 位置：RDD_SAMPLE_DIR > maa_option.json 的 rdd_sample_dir > <log_dir>/RedDotDetector_samples。
#   宿主进程对 log_dir 的重定向(如 VSCode 扩展指到 workspaceStorage)在 agent 侧
#   无 API 可读——MaaGlobalOption 只有 setter、没有 getter——故默认位置取
#   本体推算的 <root>/debug/(与 maa.log 同根)；重定向场景用 RDD_SAMPLE_DIR 明示。
#
# 节流去重：同 key(节点+ROI+结果)默认 1800s 最多一张(RDD_SAMPLE_INTERVAL 可调，硬闸，
#   防脉冲动画/自循环灌盘)；间隔过后若画面(roi_crop 哈希)没变仍不采——
#   且不刷新计时，画面一变下一次调用即采。
#
# 模块化：识别器内仅两处 hook(命中/未命中出口各一)；关掉 = RDD_SAMPLE=off，
#   拿掉 = 删本文件 + 那两处 hook。采样任何异常只 print，绝不影响识别主流程。
# ================================================================

import hashlib
import json
import os
import re
import time

import numpy as np
from PIL import Image


# 台账文件名。第一个是现行写入名；其余是历史名，只读端(回放器)须一并识别——
# 存量语料与用户手上的旧包都还是旧名，改名不能让它们作废。
MANIFEST_NAME = "samples.jsonl.log"
MANIFEST_NAMES = (MANIFEST_NAME, "samples.jsonl")


class RddSampler:
    _MODES = ("off", "fail", "all")

    def __init__(self, default_dir_fn, option_fn=None):
        """
        default_dir_fn: () -> str，默认落盘目录（由使用方注入，避免反向依赖）。
        option_fn:      () -> dict，maa_option.json 内容（读不到给 {}）。
        """
        self._default_dir_fn = default_dir_fn
        self._option_fn = option_fn or (lambda: {})
        self._mode = None            # 首次使用时定型，进程生命周期内不变
        self._dir = None
        try:
            self._interval = float(os.environ.get("RDD_SAMPLE_INTERVAL", "1800"))
        except (TypeError, ValueError):
            self._interval = 1800.0  # 环境变量非法时兜底，避免 import 阶段崩溃
        self._last_ts = {}           # key -> 上次落盘时间
        self._last_hash = {}         # key -> 上次 roi_crop 内容哈希

    # ------------------------------------------------------------------
    # 对外唯一入口
    # ------------------------------------------------------------------

    def record(self, *, node, roi, result, stage=None, images=None, meta=None):
        """
        采一条样本。node=检测点名；roi=(x,y,w,h) 全局；result="hit"/"miss"；
        stage=miss 卡点；images={tag: BGR ndarray 或 bool mask}；meta=其余 JSONL 字段。
        """
        try:
            mode = self._resolve_mode()
            if mode == "off" or (mode == "fail" and result != "miss"):
                return
            key = self._key(node, roi, result)
            now = time.time()
            if now - self._last_ts.get(key, 0.0) < self._interval:
                return
            digest = self._digest((images or {}).get("roi_crop"))
            if digest and digest == self._last_hash.get(key):
                return   # 间隔过后画面仍没变→不采也不刷新计时，画面一变下一次即采

            out_dir = self._resolve_dir()
            os.makedirs(out_dir, exist_ok=True)
            ts_tag = (time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
                      + f"{int(now * 1000) % 1000:03d}")
            files = []
            for tag, img in (images or {}).items():
                name = f"{ts_tag}_{key}_{tag}.png"
                if self._save_img(os.path.join(out_dir, name), img):
                    files.append(name)

            line = {"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                    "node": node, "result": result, "stage": stage,
                    "roi": [int(v) for v in roi]}
            line.update(meta or {})
            line["files"] = files
            with open(os.path.join(out_dir, MANIFEST_NAME), "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, default=self._jsonable) + "\n")

            self._last_ts[key] = now
            if digest:
                self._last_hash[key] = digest
        except Exception as e:
            print(f"[RddSampler] 采样失败(不影响识别): {e}")

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------

    def _opt(self) -> dict:
        try:
            return self._option_fn() or {}
        except Exception:
            return {}

    def _resolve_mode(self) -> str:
        if self._mode is None:
            env = os.environ.get("RDD_SAMPLE")
            val = env if env is not None else str(self._opt().get("rdd_sample", "all"))
            val = val.strip().lower()
            self._mode = val if val in self._MODES else "all"
        return self._mode

    def _resolve_dir(self) -> str:
        if self._dir is None:
            d = (os.environ.get("RDD_SAMPLE_DIR")
                 or self._opt().get("rdd_sample_dir")
                 or self._default_dir_fn())
            self._dir = os.path.abspath(d)
        return self._dir

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _key(node, roi, result) -> str:
        raw = f"{node or 'node'}_{'-'.join(str(int(v)) for v in roi)}_{result}"
        return re.sub(r'[^A-Za-z0-9_.\-]', '_', raw)

    @staticmethod
    def _digest(img):
        if img is None or not isinstance(img, np.ndarray):
            return None
        return hashlib.md5(img.tobytes()).hexdigest()

    @staticmethod
    def _save_img(path, img) -> bool:
        try:
            if img is None or not isinstance(img, np.ndarray) or img.size == 0:
                return False
            if img.dtype == bool:   # bool mask → 白底黑形状
                rgb = np.full((*img.shape, 3), 255, dtype=np.uint8)
                rgb[img] = [0, 0, 0]
            else:                   # BGR → RGB
                rgb = img[..., ::-1]
            Image.fromarray(rgb).save(path)
            return True
        except Exception as e:
            print(f"[RddSampler] 图片保存失败({os.path.basename(path)}): {e}")
            return False

    @staticmethod
    def _jsonable(o):
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        return str(o)
