import json
import re
import time
import os
import traceback
import numpy as np
from collections import deque
from typing import Union, Optional
from PIL import Image, ImageFilter

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.context import Context
from maa.define import RectType

from utils import mfaalog


# ================================================================
# == 模块级 HSV 工具函数（HSVShapeMatching / RedDotDetector 共用）==
# ================================================================

def _map_h(h_cv: int, is_upper: bool = False) -> int:
    """OpenCV H(0-180) → Pillow H(0-255)，下界 floor，上界 ceil。"""
    val = h_cv * (255.0 / 180.0)
    return min(255, int(np.ceil(val))) if is_upper else max(0, int(np.floor(val)))


def _compute_hsv_mask(hsv_np: np.ndarray, ranges: list) -> np.ndarray:
    """多组 HSV 阈值 OR 合并，返回 bool mask。"""
    combined = np.zeros(hsv_np.shape[:2], dtype=bool)
    for rng in ranges:
        lo = rng.get("lower") or rng.get("lower_hsv")
        hi = rng.get("upper") or rng.get("upper_hsv")
        lower_pil = np.array([_map_h(lo[0], False), lo[1], lo[2]])
        upper_pil = np.array([_map_h(hi[0], True),  hi[1], hi[2]])
        combined |= np.all((hsv_np >= lower_pil) & (hsv_np <= upper_pil), axis=-1)
    return combined


def _label_blobs(mask: np.ndarray):
    """
    BFS 连通域标注（4-邻域），无外部依赖。
    返回 (labeled_array, num_labels)，labeled_array[y,x] 为 1-based 标签，0 表示背景。
    """
    h, w = mask.shape
    labeled = np.zeros((h, w), dtype=np.int32)
    label = 0
    for sy in range(h):
        for sx in range(w):
            if not mask[sy, sx] or labeled[sy, sx]:
                continue
            label += 1
            queue = deque([(sy, sx)])
            labeled[sy, sx] = label
            while queue:
                y, x = queue.popleft()
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not labeled[ny, nx]:
                        labeled[ny, nx] = label
                        queue.append((ny, nx))
    return labeled, label


# ================================================================
# == HSV 形状匹配识别器 (HSV Shape Matching) ==
#
# [核心功能]
# 先对截图做 HSV 颜色过滤 + 二值化，再将处理后的图像交给任意 MAA 识别节点执行。
# 适用于半透明图标、低饱和度 UI 元素、受复杂背景干扰的目标。
#
# [工作流程]
# 1. 固定一帧截图 (argv.image)，全程复用，不重新截图。
# 2. 按 HSV 阈值过滤目标像素，生成"白底黑形状"掩膜图。
#    支持多组阈值 OR 合并，应对同一画面明/暗背景切换。
# 3. 将掩膜图交给"核心节点"执行 MAA 原生识别（TemplateMatch / OCR 等均可）。
# 4. 可选 edge_assist 模式：HSV 掩膜识别失败后，追加边缘 AND 掩膜再尝试一次，
#    用于剥离颜色与目标相近的平坦背景噪声。
#
# [参数说明]
#
# --- 方案 A: hsv_ranges（多组阈值 → 同一节点）---
#   target_node    (str)   必填。核心识别节点名称。
#   hsv_ranges     (list)  必填。[{lower:[H,S,V], upper:[H,S,V]}, ...]，多组 OR 合并。
#
# --- 方案 B: hsv_map（每组阈值 → 各自节点）---
#   hsv_map        (dict)  必填。{node_name: {lower,upper} 或 [{...},...]}，按序尝试。
#
# --- 旧格式（单组，兼容保留）---
#   target_node    (str)   必填。
#   lower_hsv      (list)  必填。[H, S, V] 下限。
#   upper_hsv      (list)  必填。[H, S, V] 上限。
#
# --- 通用可选参数 ---
#   edge_assist    (bool)  默认 false。HSV 失败后，追加边缘 AND 掩膜再识别一次。
#   edge_threshold (int)   默认 15。边缘强度阈值（0-255），越低越敏感。
#   debug          (bool)  默认 false。保存各阶段中间图，日志输出像素覆盖率。
#
# [HSV 坐标系]
#   配置使用 OpenCV 标准：H 0-180，S 0-255，V 0-255。
#   代码内部自动映射到 Pillow 标准（H 0-255），用户无需关心。
#   注意：红色在 HSV 中跨越 H=0，建议拆成两组阈值（如 [170,S,V]~[180,S,V]
#   和 [0,S,V]~[10,S,V]）通过多组 OR 完整覆盖，而非强行写 low > high。
#
# [调试说明]
#   debug:true 时，每次识别生成以下文件：
#     debug_{node}_{ts}_1_hsv.png    → HSV 掩膜结果（白底黑形状）
#     debug_global_{ts}_2_edge.png   → 边缘掩膜（仅 edge_assist 模式）
#     debug_{node}_{ts}_3_and.png    → AND 结果（仅 edge_assist 模式）
#   日志同步输出各阶段覆盖率，辅助判断阈值方向：
#     覆盖率 ≈  0% → HSV 范围太窄，目标被漏掉
#     覆盖率 > 50% → HSV 范围太宽，背景混入
#
# ================================================================
#
# [使用示例 A] hsv_ranges —— 多组阈值 OR 合并，对接同一个核心节点
#   适合场景：图标在明/暗背景下外观相似，同一套模板可以匹配两种情况。
#
# {
#     "FindIcon": {
#         "recognition": "Custom",
#         "custom_recognition": "HSVShapeMatching",
#         "custom_recognition_param": {
#             "target_node": "FindIcon_Core",
#             "hsv_ranges": [
#                 {"lower": [0,  0,  40], "upper": [180, 30, 180]},
#                 {"lower": [0,  0, 180], "upper": [180, 15, 255]}
#             ],
#             "edge_assist": true,
#             "edge_threshold": 15,
#             "debug": true
#         },
#         "action": "Click",
#         "next": ["NextTask"]
#     },
#     "FindIcon_Core": {
#         "recognition": "TemplateMatch",
#         "template": "Binary/icon.png",
#         "threshold": 0.6,
#         "roi": [100, 200, 80, 80]
#     }
# }
#
# [使用示例 B] hsv_map —— 每组阈值对接不同的核心节点
#   适合场景：明/暗背景下图标形状或细节差异较大，需要不同模板或不同 ROI。
#   hsv_map 中的节点按声明顺序逐一尝试，第一个命中即返回。
#
# {
#     "FindIcon_Adaptive": {
#         "recognition": "Custom",
#         "custom_recognition": "HSVShapeMatching",
#         "custom_recognition_param": {
#             "hsv_map": {
#                 "FindIcon_Core_Dark":  {"lower": [0, 0, 40],  "upper": [180, 30, 180]},
#                 "FindIcon_Core_Light": {"lower": [0, 0, 180], "upper": [180, 15, 255]}
#             },
#             "debug": false
#         },
#         "action": "Click",
#         "next": ["NextTask"]
#     },
#     "FindIcon_Core_Dark": {
#         "recognition": "TemplateMatch",
#         "template": "Binary/icon_dark_bg.png",
#         "threshold": 0.6,
#         "roi": [100, 200, 80, 80]
#     },
#     "FindIcon_Core_Light": {
#         "recognition": "TemplateMatch",
#         "template": "Binary/icon_light_bg.png",
#         "threshold": 0.55,
#         "roi": [100, 200, 80, 80]
#     }
# }
#
# ================================================================


@AgentServer.custom_recognition("HSVShapeMatching")
class HSVShapeMatching(CustomRecognition):
    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        """
        HSV 形状匹配识别器（Pillow 无 OpenCV 依赖版，兼容 Windows ARM64）

        工作流程：
        1. 固定截图 argv.image，全程复用，不重复截图。
        2. 按 HSV 阈值（支持多组 OR）生成白底黑形状掩膜图，交给目标节点识别。
        3. 若 edge_assist=true，HSV 失败后追加边缘 AND 掩膜再试一次。

        参数格式与完整示例见模块顶部注释块。
        """
        try:
            # 0. 固定截图，全程复用
            original_bgr = argv.image
            ts = f"{time.time():.3f}".replace('.', '_')

            # 1. 解析参数
            raw = argv.custom_recognition_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))

            debug_mode     = params.get("debug", False)
            edge_assist    = params.get("edge_assist", False)
            edge_threshold = params.get("edge_threshold", 15)

            # 2. BGR → PIL → HSV（只做一次）
            pil_img = Image.fromarray(original_bgr[..., ::-1])  # BGR → RGB → PIL
            hsv_np  = np.array(pil_img.convert("HSV"))

            # 3. 解析任务列表 [(node_name, [ranges])]
            tasks = self._parse_tasks(params)
            if not tasks:
                mfaalog.error("[HSVShapeMatching] 参数错误：未找到有效节点配置")
                return None

            # 4. 预计算 edge_mask（全局只算一次）
            edge_mask = None
            if edge_assist:
                edge_mask = self._compute_edge_mask(pil_img, edge_threshold)
                if debug_mode:
                    edge_vis = self._mask_to_bgr(original_bgr, edge_mask)
                    self._save_debug(edge_vis, "global", ts, "2_edge", edge_mask)

            # 5. 逐节点尝试
            for node_name, ranges in tasks:
                # 生成 HSV 掩膜（多范围 OR）
                hsv_mask      = _compute_hsv_mask(hsv_np, ranges)
                processed_hsv = self._mask_to_bgr(original_bgr, hsv_mask)

                if debug_mode:
                    self._save_debug(processed_hsv, node_name, ts, "1_hsv", hsv_mask)

                # 尝试 HSV 掩膜识别
                result = self._try_recognition(context, node_name, processed_hsv, "HSV")
                if result:
                    return result

                # edge_assist 升级：AND 掩膜
                if edge_assist and edge_mask is not None:
                    and_mask      = hsv_mask & edge_mask
                    processed_and = self._mask_to_bgr(original_bgr, and_mask)
                    if debug_mode:
                        self._save_debug(processed_and, node_name, ts, "3_and", and_mask)
                    result = self._try_recognition(context, node_name, processed_and, "AND")
                    if result:
                        return result

            return None

        except Exception:
            mfaalog.error(f"[HSVShapeMatching] 执行异常:\n{traceback.format_exc()}")
            return None

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _parse_tasks(self, params: dict) -> list:
        """解析参数，返回 [(node_name, [ranges])] 列表。"""
        if "hsv_map" in params:
            # 方案 B
            return [
                (node, spec if isinstance(spec, list) else [spec])
                for node, spec in params["hsv_map"].items()
            ]

        # 方案 A 或旧格式
        node = params.get("target_node") or params.get("recognition")
        if not node:
            return []
        if "hsv_ranges" in params:
            ranges = params["hsv_ranges"]
        else:
            ranges = [{"lower": params.get("lower_hsv", [0, 0, 120]),
                       "upper": params.get("upper_hsv", [180, 50, 255])}]
        return [(node, ranges)]

    def _compute_edge_mask(self, pil_img: Image.Image, threshold: int) -> np.ndarray:
        """基于 Pillow FIND_EDGES 生成边缘 bool mask。"""
        edge_np = np.array(pil_img.convert("L").filter(ImageFilter.FIND_EDGES))
        return edge_np > threshold

    def _mask_to_bgr(self, original_bgr: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """将 mask 渲染为白底黑形状的 BGR 图（目标区域=黑，其余=白）。"""
        result = np.full_like(original_bgr, 255)
        result[mask] = [0, 0, 0]
        return result

    def _save_debug(self, bgr_img: np.ndarray, node_name: str, ts: str,
                    stage: str, mask: np.ndarray) -> None:
        """保存调试图，并在日志打印像素覆盖率。"""
        hit = int(np.sum(mask))
        pct = hit / mask.size * 100
        try:
            debug_dir = "debug_images"
            os.makedirs(debug_dir, exist_ok=True)
            safe_node = re.sub(r'[<>:"/\\|?*]', '_', node_name)
            filename  = f"{debug_dir}/debug_{safe_node}_{ts}_{stage}.png"
            Image.fromarray(bgr_img[..., ::-1]).save(filename)
            mfaalog.info(f"[HSVShapeMatching] [{stage}] {node_name} 覆盖 {hit}px ({pct:.1f}%) → {filename}")
        except Exception as e:
            mfaalog.warning(f"[HSVShapeMatching] [{stage}] {node_name} 覆盖 {hit}px ({pct:.1f}%) | 调试图保存失败: {e}")

    def _try_recognition(self, context: Context, node_name: str,
                         processed_bgr: np.ndarray, stage: str) -> Optional[CustomRecognition.AnalyzeResult]:
        """调用 MAA 识别节点，命中返回 AnalyzeResult，否则返回 None。"""
        detail = context.run_recognition(node_name, processed_bgr)
        if detail and detail.hit:
            mfaalog.debug(f"[HSVShapeMatching] [{stage}] 命中: {node_name}")
            return CustomRecognition.AnalyzeResult(box=detail.box, detail=detail.raw_detail)
        return None


# ================================================================
# == 红点感叹号识别器 (Red Dot Detector) ==
#
# [核心功能]
# 在任意背景下稳健识别游戏 UI 中的红色通知点（内含白色感叹号）。
# 不依赖 TemplateMatch，通过颜色拓扑 + 垂直投影分析确认感叹号结构。
#
# [两种模式]
#   独立模式：节点自带完整参数，直接执行。
#   预设模式：节点只写 {"preset": "节点名"}，复用预设节点参数；命中坐标自动加回 roi 偏移。
#             调用者节点名 + ROI 会透传给预设节点，用于失败截图命名（见可观测性）。
#
# [识别原理 —— 置信度加权模型]
#   红块过面积后，取"被红包围的非红区"(enclosed，拓扑封闭，不卡绝对亮度，抗压暗)，
#   对其打一个 0~1 的置信分，≥ min_confidence 即命中。各分项均为归一化、抗模糊量：
#     f_gap   连续断层(带两侧门)：清晰帧(PC)主力特征，命中可单独拉满；模糊时自动归零。
#     f_vert  纵横比 h/w：模糊地板主力，扛得住"等高实心柱"(如投影 [2,2,2,2])，排圆形高光。
#     f_white 偏白对比：内部 (V/255)(1-S/255) 中位数 − 红环中位数；S 为主、V 为辅，抗压暗。
#     f_cent  居中：内部水平中心 vs 红块中心，小补充。
#   权重见模块常量 _W_*；conf 与各分项写入 detail.stat，调参有数据支撑。
#
# [参数说明]（HSV 坐标系：OpenCV 标准 H 0-180 / S,V 0-255，内部自动映射 Pillow）
#   hsv_ranges     (list)  红色 HSV 范围 [{lower:[H,S,V], upper:[H,S,V]}, ...]，H 跨 0 拆两组 OR。
#   red_area       (list)  红色 blob 面积范围 [min, max]，默认 [30, 1200]。
#   min_confidence (float) 命中阈值(0-1)，默认 0.25(精准 ROI 调好值)，作者显式指定；
#                          大 ROI 泛找务必调高(如 0.5+)，避免杂红误判。
#   gap_ratio      (float) 仅用于 detail 里 gap 的"是否成双段"标注，不再作命中门槛。
#   preset         (str)   预设节点名（预设模式）。
#   注：旧的 inner_v_min / inner_s_max 已弃用(绝对亮度阈值在模糊下会掉崖)，若残留会被忽略。
#
# [可观测性 —— 常驻，无需任何开关]
#   命中失败时：
#     · detail 写入 {stage, hint, stat}；stat 含 conf / parts(各分项) / proj(投影) / gap，
#       随 MAA 识别记录进入日志分析工具(MaaLogAnalyzer / MaaLogs)，图没了也能复盘。
#     · mfaalog.warning 输出一行精简摘要(上 UI)；print 输出明细与截图路径(仅进 txt 日志)。
#     · 落盘 roi_crop / red_mask / inner(封闭区) 三张小图(各约几百字节)。
#   截图位置：自动写入 UI 日志目录(maa.log 同级)下的 RedDotDetector/ 子目录——
#     · interface 本级(Agent CWD)有 debug 或 config → 用户侧(interface 在根)，用本级 debug；
#     · 否则 → 开发侧(interface 在 assets)，取上一级 debug。
#     · 环境变量 RDD_DEBUG_DIR 可强制指定日志目录(仍自动建 RedDotDetector 子目录)。
#   防自循环刷屏：文件名 = 节点名+ROI(同检测点重复失败直接覆盖，文件数与循环次数无关)，
#     另加同检测点时间节流(默认 2s，环境变量 RDD_DUMP_INTERVAL 可调)。
#
# [一句话调参口诀]（对照 detail.stage）
#   red_mask    → HSV 没框到红色：降低 S/V 下限 / 校正 roi
#   area        → 面积不在 red_area：多半 min 太大
#   interior    → 红块内无封闭非红区(无感叹号轮廓)：roi 偏移 / 红圈破损 / 被模糊填满
#   confidence  → 有候选但分不够：看 stat.conf 与 parts，降 min_confidence 提召回
#
# ================================================================
#
# [示例 A] 独立模式
# {
#     "CheckRedDot": {
#         "recognition": "Custom",
#         "custom_recognition": "RedDotDetector",
#         "custom_recognition_param": {
#             "hsv_ranges": [
#                 {"lower": [0,   140, 120], "upper": [12,  255, 255]},
#                 {"lower": [165, 140, 120], "upper": [180, 255, 255]}
#             ],
#             "red_area": [30, 1200], "min_confidence": 0.25
#         },
#         "roi": [950, 100, 40, 600], "action": "Click", "next": ["NextTask"]
#     }
# }
#
# [示例 B] 预设模式（多面板共用一套参数，仅 roi 不同）
# {
#     "RedDot_Preset": {
#         "recognition": "Custom",
#         "custom_recognition": "RedDotDetector",
#         "custom_recognition_param": {
#             "hsv_ranges": [
#                 {"lower": [0,   140, 120], "upper": [12,  255, 255]},
#                 {"lower": [165, 140, 120], "upper": [180, 255, 255]}
#             ],
#             "red_area": [30, 1200], "min_confidence": 0.25
#         }
#     },
#     "CheckPanel_A": {
#         "recognition": "Custom",
#         "custom_recognition": "RedDotDetector",
#         "custom_recognition_param": {"preset": "RedDot_Preset"},
#         "roi": [640, 616, 17, 16], "action": "Click", "next": ["AfterClick"]
#     }
# }
#
# ================================================================
# [调参指南] 识别原理与逐步排查
# ================================================================
#
# 失败时自动落盘（无需任何开关，覆盖写入，时间节流防刷屏）：
#   rdd_*_roi_crop.png  → 代码实际处理的裁剪区域
#   rdd_*_red_mask.png  → 红色掩膜（黑=红色，白=非红）
#   rdd_*_inner.png     → 最佳候选 blob 的封闭内部区（黑=enclosed）
# 文件位置：debug/RedDotDetector/ 目录（maa.log 同级，RDD_DEBUG_DIR 可强制指定）。
# 文件名以"节点名+ROI"为 key，同检测点重复失败直接覆盖。
#
# 失败原因结构化写入 detail，通过 MaaLogs 工具可复盘（图没了也能看数据）：
#   detail.stage           卡在哪个阶段（red_mask / area / interior / confidence）
#   detail.hint            具体提示与修正方向
#   detail.stat.conf       最高候选置信分（confidence 阶段专用）
#   detail.stat.parts      各分项得分 {gap, vert, white, cent}
#   detail.stat.proj       垂直投影数组（按行的封闭区像素数，用于判断双段）
#   detail.stat.gap        断层数值 {row, val, peak, ratio, above_nz, has_below}
#
# ────────────────────────────────────────────────────────────────
# 阶段 1  ROI 裁剪
# ────────────────────────────────────────────────────────────────
# 参数：任务 JSON 的顶层 roi 字段（不在 custom_recognition_param 里）
# 输出：rdd_*_roi_crop.png  ← 代码实际处理的像素区域
#
# 排查：打开 roi_crop.png，红点必须完整在图内。
#       若图里没有红点 → 坐标填错了，对着游戏原图重新量取。
#       建议 roi 比红点略大 2-4px；精准 ROI 还能抑制大面积杂红误判。
#
# ────────────────────────────────────────────────────────────────
# 阶段 2  HSV 过滤 → 红色掩膜（detail.stage = "red_mask"）
# ────────────────────────────────────────────────────────────────
# 参数：hsv_ranges（H 0-180 / S 0-255 / V 0-255，OpenCV 坐标系）
# 输出：rdd_*_red_mask.png  ← 黑=检测为红，白=非红
#
# 调参方法：
#   在游戏截图上拾取红点边缘 3-4 个像素，记录 RGB → 转 HSV；
#   各分量取最值后留 10-20 的余量作阈值范围。
#   注意：游戏红色常跨越 H=0（H 在 170-180 和 0-10 各有一段），
#   须拆成两组 OR 合并；单组 lower > upper 无效。
#   红色连片（红点与其他红色 UI 连成一块）不影响识别：感叹号的拓扑关系不变。
#
# 判断（看 red_mask.png）：
#   全白             → hsv_ranges 没覆盖到实际红色，降 S/V 下限
#   大片黑色（背景黑）→ hsv_ranges 太宽，提高 S 或 V 下限
#   菱形轮廓完整黑色 → 正常，进入下一阶段
#
# ────────────────────────────────────────────────────────────────
# 阶段 3  连通域面积筛选（detail.stage = "area"）
# ────────────────────────────────────────────────────────────────
# 参数：red_area [min, max]
# 数据：detail.stat.n_blobs（总连通域数）、detail.stat.area_pass（通过面积的数量）
#
# 面积估算：菱形面积 ≈ 对角线² / 2。16px 菱形 ≈ 128px，10px 菱形 ≈ 50px。
# 建议 min=30，max=1200；场景有大面积红色 UI 元素时适当调小 max。
# area_pass=0 → 所有 blob 被过滤，多半 min 太大。
#
# ────────────────────────────────────────────────────────────────
# 阶段 4a  拓扑封闭取内部区（detail.stage = "interior"）
# ────────────────────────────────────────────────────────────────
# 输出：rdd_*_inner.png  ← 黑=enclosed（被红色真正包围的非红像素）
#
# 原理：对 blob 包围框内的非红像素做连通域标注，凡能从矩形边框触达的
# 连通域视为"外侧背景"，其余即 enclosed。此步骤不卡任何绝对亮度——
# 被模糊压暗的感叹号白色同样会被收进来，"能不能区分"是后续打分的事。
# 旧版 inner_v_min / inner_s_max 绝对阈值在此移除，掉崖问题由此解决。
#
# 卡住原因（max_inner_px = 0，inner.png 全白）：
#   · roi 偏移或太小，红圈不完整，边框漏到红圈外侧
#   · 极度模糊时红色"填满"感叹号缝隙，封闭区消失（物理边界，无法靠参数解决）
#
# ────────────────────────────────────────────────────────────────
# 阶段 4b  置信度加权评分（detail.stage = "confidence"）
# ────────────────────────────────────────────────────────────────
# 参数：min_confidence（默认 0.25，精准 ROI 调好值；大 ROI 泛找须显式调高到 0.5+）
# 数据：detail.stat.conf（最高候选分）、detail.stat.parts（各分项得分）
#
# 四个分项（权重在模块常量 _W_* 可直接调整）：
#
#   f_gap  (权重 0.45)  连续断层深度（带两侧门）
#     计算：1 - gap行像素数/投影峰值；要求断层上方≥2行且下方有像素，否则归零。
#     清晰帧（PC）：感叹号竖线与圆点之间有 1px 红色间隔，f_gap 可贡献满额 0.45。
#     模糊帧（安卓）：间隔被模糊填平 → f_gap = 0，由 f_vert 兜底。
#
#   f_vert (权重 0.35)  内部封闭区纵横比：clip(h/w − 1, 0, 1)
#     感叹号竖长，h/w 通常 2-4 → f_vert 接近 1。
#     圆形高光（h/w ≈ 1）→ f_vert ≈ 0；等高实心柱（proj=[2,2,2,2]，h/w≈2）→ f_vert≈1
#     但等高柱无 gap，总分约 0.27，仍在默认阈值 0.25 以上，保留召回。
#     模糊态区分"竖条 vs 圆形高光"的主轴，权重第二高。
#
#   f_white (权重 0.15)  偏白对比：内部 whiteness 中位数 − 红环 whiteness 中位数
#     whiteness = (V/255)(1−S/255)，S 为主 V 为辅。
#     模糊压暗时差值平滑衰减（不像绝对阈值那样掉崖）。
#     感叹号白色 whiteness ≈ 0.5，红环 ≈ 0.2，实测 f_white ≈ 0.3。
#     仅作辅证；抬高此权重意义不大，感叹号 whiteness 和圆形高光差距不如 vert 稳定。
#
#   f_cent (权重 0.05)  内部水平居中程度：小补充，通常接近 1。
#
# 典型分值参考（实测 16px 精准 ROI）：
#   真实感叹号(清晰)：gap≈0.80, vert≈0.70, white≈0.30, cent≈1.0 → conf ≈ 0.65
#   真实感叹号(模糊)：gap= 0,   vert≈0.70, white≈0.30, cent≈1.0 → conf ≈ 0.32
#   等高实心柱(最差)：gap= 0,   vert≈0.50, white≈0.30, cent≈0.9 → conf ≈ 0.27
#   圆形高光：        gap= 0,   vert≈ 0,   white≈0.30, cent≈0.8 → conf ≈ 0.09
#   默认阈值 0.25 卡在感叹号(最弱)≈0.27 与圆形高光≈0.09 之间，有安全余量。
#
# 调参方法：
#   先看 detail.stat.conf，再看 detail.stat.parts 定位哪项拉低了：
#     gap=0, vert 正常   → 模糊帧，f_vert 兜底。conf 略低于阈值时直接降 min_confidence。
#     gap=0, vert 也低   → 感叹号被模糊成类圆形，或 roi 太大内部被填满；
#                          缩小 roi 让纵横比更高，或接受漏检（物理边界）。
#     white 低（<0.1）   → 内部与红环颜色接近（少见）；white 仅辅证，先降阈值。
#   大 ROI 泛找务必调高 min_confidence 到 0.5+，
#   否则大面积连通域内的偶发竖向结构会误判。
#
# ────────────────────────────────────────────────────────────────
# 物理边界说明
# ────────────────────────────────────────────────────────────────
# 模糊程度 = 红色完全填满感叹号缝隙时：enclosed 为空，识别无解，任何参数均无效。
# 在此之前的连续过渡区（等高实心柱 conf≈0.27），可通过降 min_confidence 兜住，
# 代价是稍高的误判率（需配合精准 roi 抑制）。
#
# ────────────────────────────────────────────────────────────────
# 一句话调参口诀
# ────────────────────────────────────────────────────────────────
# roi_crop 没红点              → 改 roi 坐标
# red_mask 全白                → 降 hsv_ranges S/V 下限
# detail.stat.area_pass = 0   → 降 red_area min（或升 max）
# inner.png 全白空图            → 红圈破损或模糊填满，缩小 roi / 等清晰帧
# conf 有数但不够阈值           → 看 parts：vert 低则缩 roi；直接降 min_confidence
# 大 ROI 误判杂红               → 提高 min_confidence 到 0.5+
#
# ================================================================

_RED_RANGES_DEFAULT = [
    {"lower": [0,   130, 100], "upper": [12,  255, 255]},
    {"lower": [165, 130, 100], "upper": [180, 255, 255]},
]

# 置信度加权（可按需微调）。无 gap 时上限 = _W_VERT+_W_WHITE+_W_CENT = 0.55；
# 清晰帧 f_gap 可额外贡献至 _W_GAP，单独足以越过默认阈值。
# vert(纵横比)是模糊态区分"竖条 vs 圆形高光"的主轴，故权重最高；white 仅作辅证。
_W_GAP = 0.45      # 连续断层：清晰帧(PC)主力，带两侧门，模糊归零
_W_VERT = 0.35     # 纵横比：模糊地板主力，扛等高实心柱、排圆形高光
_W_WHITE = 0.15    # 偏白对比(S 为主、V 为辅，相对红环)：抗压暗的辅证
_W_CENT = 0.05     # 居中：小补充
_DEFAULT_MIN_CONF = 0.25   # 精准 ROI 感叹号默认阈值；大 ROI 泛找请显式调高

# 同一检测点(节点名+ROI)两次落盘的最小间隔(秒)，防 next 自循环刷屏；RDD_DUMP_INTERVAL 可调
try:
    _DUMP_MIN_INTERVAL = float(os.environ.get("RDD_DUMP_INTERVAL", "2.0"))
except (TypeError, ValueError):
    _DUMP_MIN_INTERVAL = 2.0   # 环境变量非法时兜底，避免 import 阶段崩溃中断 Agent 启动
_RESOLVED_LOG_DIR = None


def _resolve_log_dir() -> str:
    """
    定位 UI 日志目录(maa.log 所在那层)。判定规则：
      · 环境变量 RDD_DEBUG_DIR 指定 → 直接用；
      · 否则看 interface 本级(Agent CWD)是否有 debug / config：
          有 → 用户侧(interface 在根)，根 = 本级；
          无 → 开发侧(interface 在 assets)，根 = 上一级。
      最终返回 根/debug。结果缓存(CWD 启动后不变)。
    """
    global _RESOLVED_LOG_DIR
    if _RESOLVED_LOG_DIR is not None:
        return _RESOLVED_LOG_DIR

    env = os.environ.get("RDD_DEBUG_DIR")
    if env:
        _RESOLVED_LOG_DIR = env
        return env

    cwd = os.path.abspath(os.getcwd())  # = interface.json 所在目录
    has_marker = (os.path.isdir(os.path.join(cwd, "debug"))
                  or os.path.isdir(os.path.join(cwd, "config")))
    root = cwd if has_marker else os.path.dirname(cwd)
    _RESOLVED_LOG_DIR = os.path.join(root, "debug")
    return _RESOLVED_LOG_DIR


@AgentServer.custom_recognition("RedDotDetector")
class RedDotDetector(CustomRecognition):
    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        """红点感叹号识别器。参数格式与完整示例见模块顶部注释块。"""
        try:
            raw = argv.custom_recognition_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))

            if "preset" in params:
                return self._run_preset(context, argv, params)
            return self._run_standalone(argv, params)

        except Exception:
            tb = traceback.format_exc()
            mfaalog.error(f"[RedDotDetector] 执行异常:\n{tb}")
            return CustomRecognition.AnalyzeResult(box=None, detail={
                "result": "error",
                "error": tb.strip().splitlines()[-1],
            })

    # ------------------------------------------------------------------
    # 预设模式
    # ------------------------------------------------------------------

    def _run_preset(self, context: Context,
                    argv: CustomRecognition.AnalyzeArg, params: dict):
        """
        预设模式：将 argv.roi 裁剪后交给预设节点执行，结果坐标加回偏移。
        把"调用者节点名 + 原始 ROI"暂存到 self，供同实例、同步执行的嵌套独立模式读取，
        使失败截图以调用节点名(如 CheckPanel_A)命名。
        """
        preset_node = params["preset"]
        roi = argv.roi
        if roi is not None:
            rx, ry, rw, rh = roi.x, roi.y, roi.w, roi.h
        else:
            rx = ry = rw = rh = 0

        if rw > 0 and rh > 0:
            cropped = argv.image[ry:ry + rh, rx:rx + rw]
        else:
            cropped = argv.image
            rx = ry = 0

        self._caller = (getattr(argv, "node_name", "") or preset_node, (rx, ry, rw, rh))
        try:
            reco = context.run_recognition(preset_node, cropped)
        finally:
            self._caller = None

        # reco is None：识别根本没跑起来(预设节点名写错/被禁用/图像空) —— 配置错误，与漏检区分
        if reco is None:
            mfaalog.error(f"[RedDotDetector] preset 未启动: {preset_node}（节点不存在/被禁用/图像空？）")
            return CustomRecognition.AnalyzeResult(box=None, detail={
                "result": "error", "mode": "preset", "preset": preset_node,
                "roi": [rx, ry, rw, rh],
                "error": f"preset node not started: {preset_node}",
            })

        if reco.hit:
            bx, by, bw, bh = reco.box
            adjusted = (bx + rx, by + ry, bw, bh)
            mfaalog.info(f"[RedDotDetector] [preset:{preset_node}] hit -> {adjusted}")
            return CustomRecognition.AnalyzeResult(
                box=adjusted, detail={"result": "hit", "preset": preset_node})

        # 真未命中：阶段原因已由预设节点(独立模式)记进嵌套识别记录；这里附带透传其 raw_detail
        raw = getattr(reco, "raw_detail", None)
        mfaalog.warning(f"[RedDotDetector] miss@preset | {argv.node_name} via {preset_node}")
        return CustomRecognition.AnalyzeResult(box=None, detail={
            "result": "miss", "mode": "preset", "preset": preset_node,
            "roi": [rx, ry, rw, rh],
            "preset_detail": raw,
            "hint": "阶段原因见预设节点(独立模式)的 detail；失败截图见 debug/RedDotDetector/ 下以本节点名命名的 rdd_* 文件",
        })

    # ------------------------------------------------------------------
    # 独立模式
    # ------------------------------------------------------------------

    def _run_standalone(self, argv: CustomRecognition.AnalyzeArg, params: dict):
        """独立模式：HSV 过滤 → blob 面积筛选 → 拓扑封闭取内部 → 置信度加权打分。"""
        hsv_ranges = params.get("hsv_ranges", _RED_RANGES_DEFAULT)
        area_min, area_max = params.get("red_area", [30, 1200])
        gap_ratio = params.get("gap_ratio", 0.35)      # 仅用于 detail 的 gap 标注
        min_conf = params.get("min_confidence", _DEFAULT_MIN_CONF)

        # 1. 按 roi 裁剪
        roi = argv.roi
        if roi is not None:
            rx, ry, rw, rh = roi.x, roi.y, roi.w, roi.h
        else:
            rx = ry = rw = rh = 0
        if rw > 0 and rh > 0:
            work_bgr = argv.image[ry:ry + rh, rx:rx + rw]
        else:
            work_bgr = argv.image
            rx = ry = 0

        # 预设模式下用调用者(CheckPanel_A)的名字与 ROI 命名截图；否则用自身
        caller = getattr(self, "_caller", None)
        if caller:
            node, key_roi = caller
        else:
            node, key_roi = (getattr(argv, "node_name", "") or ""), (rx, ry, rw, rh)

        # 2. HSV → 红色掩膜
        pil_img = Image.fromarray(work_bgr[..., ::-1])
        hsv_np = np.array(pil_img.convert("HSV"))
        red_mask = _compute_hsv_mask(hsv_np, hsv_ranges)

        # 3. 连通域
        labeled, n_blobs = _label_blobs(red_mask)
        stat = {"red_px": int(red_mask.sum()), "n_blobs": int(n_blobs),
                "area_pass": 0, "max_inner_px": 0, "scored": 0}
        best, best_mask = None, None

        # 4. 逐 blob 检测
        for i in range(1, n_blobs + 1):
            blob = (labeled == i)
            area = int(np.sum(blob))
            if not (area_min <= area <= area_max):
                continue
            stat["area_pass"] += 1

            rows = np.where(np.any(blob, axis=1))[0]
            cols = np.where(np.any(blob, axis=0))[0]
            bx0, bx1 = int(cols[0]), int(cols[-1])
            by0, by1 = int(rows[0]), int(rows[-1])
            bw, bh = bx1 - bx0 + 1, by1 - by0 + 1

            box_red = red_mask[by0:by1 + 1, bx0:bx1 + 1]
            box_hsv = hsv_np[by0:by1 + 1, bx0:bx1 + 1]

            # 拓扑封闭过滤：排除矩形四角能触达边框的背景，只留被红色真正包围的内部
            # 不卡绝对亮度——白被模糊压暗也照取，由偏白"对比"在打分时衡量
            non_red_crop = ~box_red
            labeled_crop, _ = _label_blobs(non_red_crop)
            border_labels = (set(labeled_crop[0, :].tolist()) | set(labeled_crop[-1, :].tolist())
                             | set(labeled_crop[:, 0].tolist()) | set(labeled_crop[:, -1].tolist()))
            border_labels.discard(0)
            enclosed = non_red_crop & ~np.isin(labeled_crop, list(border_labels))

            enc_px = int(enclosed.sum())
            stat["max_inner_px"] = max(stat["max_inner_px"], enc_px)
            if enc_px == 0:
                continue

            chk = self._exclamation_info(enclosed, gap_ratio)       # 投影/断层(供 f_gap 与诊断)
            conf, parts = self._confidence(box_hsv, box_red, enclosed, chk)
            if best is None or conf > best["conf"]:
                best = {"conf": conf, "parts": parts, **chk}
                best_mask = enclosed

            stat["scored"] += 1
            if conf >= min_conf:
                result_box = (bx0 + rx, by0 + ry, bw, bh)
                mfaalog.info(f"[RedDotDetector] hit | box={result_box} conf={conf} {parts}")
                return CustomRecognition.AnalyzeResult(
                    box=result_box,
                    detail={"result": "hit", "conf": conf, "parts": parts,
                            "red_area": area, "box": list(result_box)})

        # 5. 未命中：置信/投影/断层进 stat → detail；落盘失败图；统一出口
        if best is not None:
            stat["conf"] = best["conf"]
            stat["parts"] = best["parts"]
            stat["proj"] = best["proj"]
            stat["gap"] = {"row": best["gap_row"], "val": best["gap_val"], "peak": best["peak"],
                           "ratio": best["ratio"], "above_nz": best["above_nz"],
                           "has_below": best["has_below"]}

        stage, hint = self._diagnose(stat, area_min, area_max, min_conf)
        self._dump_failure(node, key_roi, work_bgr, red_mask, best_mask)
        return self._miss("standalone", stage, hint, stat, params)

    # ------------------------------------------------------------------
    # 感叹号结构检测：返回投影 + 断层诊断信息
    # ------------------------------------------------------------------

    def _exclamation_info(self, region: np.ndarray, gap_ratio: float) -> dict:
        """
        从内部封闭区的垂直投影提取断层信息（供 f_gap 与诊断使用）。
        返回 {pass, proj, gap_row, gap_val, peak, ratio, above_nz, has_below}。
        pass 仅作"是否成清晰双段"的标注，命中与否由 _confidence 决定。
        """
        proj_arr = np.sum(region, axis=1).astype(np.float32)
        info = {"pass": False, "proj": proj_arr.astype(int).tolist(),
                "gap_row": None, "gap_val": None,
                "peak": int(proj_arr.max()) if proj_arr.size else 0,
                "ratio": None, "above_nz": 0, "has_below": False}

        if int(proj_arr.sum()) < 3:
            return info
        nz = np.where(proj_arr > 0)[0]
        if len(nz) < 2:
            return info

        first_nz, last_nz = int(nz[0]), int(nz[-1])
        trimmed = proj_arr[first_nz:last_nz + 1]  # 去掉包围框上下空白行
        if len(trimmed) < 3:                      # 区段太短：长宽比兜底
            ph, pw = region.shape
            info["pass"] = ph > pw * 1.3
            return info

        gap_abs = first_nz + int(np.argmin(trimmed[1:-1])) + 1   # 断层行(原始下标)
        peak = float(proj_arr.max())
        if peak == 0:
            return info

        ratio = float(proj_arr[gap_abs] / peak)
        above_nz = int(np.sum(proj_arr[first_nz:gap_abs] > 0))           # 竖线段非零行数
        has_below = bool(np.any(proj_arr[gap_abs + 1:last_nz + 1] > 0))  # 圆点段是否有像素
        info.update({"gap_row": gap_abs + 1, "gap_val": int(proj_arr[gap_abs]),
                     "peak": int(peak), "ratio": round(ratio, 3),
                     "above_nz": above_nz, "has_below": has_below,
                     "pass": (ratio <= gap_ratio) and above_nz >= 2 and has_below})
        return info

    # ------------------------------------------------------------------
    # 置信度加权：gap(连续) + 纵横比 + 偏白对比 + 居中
    # ------------------------------------------------------------------

    @staticmethod
    def _whiteness(V: np.ndarray, S: np.ndarray) -> np.ndarray:
        """偏白程度 ∈[0,1]：亮(V↑)且低饱和(S↓)。"""
        return (V.astype(np.float32) / 255.0) * (1.0 - S.astype(np.float32) / 255.0)

    def _confidence(self, box_hsv: np.ndarray, box_red: np.ndarray,
                    enclosed: np.ndarray, chk: dict):
        """对一个候选打 0~1 置信分，返回 (conf, parts)。各分项均为归一化抗模糊量。"""
        ys, xs = np.where(enclosed)
        if len(ys) < 2:
            return 0.0, {"by": "no_inner"}

        # f_gap：连续断层。带两侧门(谷上下都得有料)，渐细收尾不算断层；模糊实心柱→0
        peak = chk.get("peak") or 0
        if peak and chk.get("has_below") and chk.get("above_nz", 0) >= 2:
            f_gap = float(np.clip(1.0 - chk["gap_val"] / peak, 0.0, 1.0))
        else:
            f_gap = 0.0

        # f_vert：纵横比 h/w。扛得住等高实心柱([2,2,2,2]→h/w=2)，排圆形高光(≈1→0)
        h = int(ys.max() - ys.min() + 1)
        w = int(xs.max() - xs.min() + 1)
        f_vert = float(np.clip(h / max(w, 1) - 1.0, 0.0, 1.0))

        # f_white：偏白"对比"。内部中位数 − 红环中位数；模糊压暗时差值平滑衰减，不掉崖
        V, S = box_hsv[..., 2], box_hsv[..., 1]
        w_in = float(np.median(self._whiteness(V[enclosed], S[enclosed])))
        w_rng = float(np.median(self._whiteness(V[box_red], S[box_red]))) if box_red.any() else 0.0
        f_white = float(np.clip(w_in - w_rng, 0.0, 1.0))

        # f_cent：内部水平中心 vs 包围框中心
        cx_e = (int(xs.min()) + int(xs.max())) / 2.0
        cx_c = (enclosed.shape[1] - 1) / 2.0
        f_cent = float(np.clip(1.0 - 2.0 * abs(cx_e - cx_c) / max(enclosed.shape[1], 1), 0.0, 1.0))

        conf = _W_GAP * f_gap + _W_VERT * f_vert + _W_WHITE * f_white + _W_CENT * f_cent
        parts = {"gap": round(f_gap, 2), "vert": round(f_vert, 2),
                 "white": round(f_white, 2), "cent": round(f_cent, 2)}
        return round(float(conf), 3), parts

    # ------------------------------------------------------------------
    # 失败诊断 / 统一出口
    # ------------------------------------------------------------------

    def _diagnose(self, stat: dict, area_min, area_max, min_conf):
        """根据累加器判定卡在哪个阶段，给出修正方向。"""
        if stat["red_px"] == 0:
            return "red_mask", "HSV 未覆盖到任何红色，降低 S/V 下限，或确认 roi 框住了红点"
        if stat["n_blobs"] == 0:
            return "red_mask", f"有红像素但未成连通域(red_px={stat['red_px']})，检查红色是否破碎"
        if stat["area_pass"] == 0:
            return "area", f"红色面积都不在 [{area_min},{area_max}]，调 red_area（多半是 min 太大）"
        if stat["max_inner_px"] == 0:
            return "interior", "红块内无封闭非红区(无感叹号轮廓)：roi 偏移 / 红圈破损 / 被模糊填满"
        return "confidence", (f"最高置信 {stat.get('conf')} < min_confidence({min_conf})；"
                              f"分项 {stat.get('parts')}；降低 min_confidence 提召回，"
                              f"或检查偏白/竖向是否被模糊吃掉")

    def _miss(self, mode: str, stage: str, hint: str, stat: dict, params: dict):
        """统一失败出口：精简摘要进 mfaalog(上 UI)，明细 print(仅 txt)，结构化原因进 detail。"""
        detail = {
            "result": "miss", "mode": mode, "stage": stage, "hint": hint, "stat": stat,
            "params": {k: params.get(k) for k in
                       ("hsv_ranges", "red_area", "gap_ratio", "min_confidence")},
        }
        mfaalog.warning(f"[RedDotDetector] miss@{stage} | {hint}")
        print(f"[RedDotDetector] miss stat={stat}")
        return CustomRecognition.AnalyzeResult(box=None, detail=detail)

    # ------------------------------------------------------------------
    # 调试图：常驻、固定命名(覆盖)、时间节流；路径走 print
    # ------------------------------------------------------------------

    def _bool_to_bgr(self, mask: np.ndarray) -> np.ndarray:
        """bool mask → 白底黑形状 BGR 图。"""
        out = np.full((*mask.shape, 3), 255, dtype=np.uint8)
        out[mask] = [0, 0, 0]
        return out

    def _img_key(self, node_name: str, roi_tuple) -> str:
        """文件名 key = 节点名 + ROI。同检测点重复失败 → 同名覆盖；不同面板 ROI 不同 → 不互相覆盖。"""
        raw = f"{node_name or 'node'}_{roi_tuple[0]}-{roi_tuple[1]}-{roi_tuple[2]}-{roi_tuple[3]}"
        return re.sub(r'[^A-Za-z0-9_.\-]', '_', raw)

    def _save_debug_img(self, bgr_img: np.ndarray, key: str, tag: str):
        try:
            debug_dir = os.path.join(_resolve_log_dir(), "RedDotDetector")
            os.makedirs(debug_dir, exist_ok=True)
            path = os.path.abspath(os.path.join(debug_dir, f"rdd_{key}_{tag}.png"))
            Image.fromarray(bgr_img[..., ::-1]).save(path)  # 同名覆盖
            return path
        except Exception as e:
            print(f"[RedDotDetector] 调试图保存失败({tag}): {e}")
            return None

    def _dump_failure(self, node_name, roi_tuple, work_bgr, red_mask, inner_best):
        """失败常驻图：roi_crop + red_mask (+ inner)。固定名覆盖 + 时间节流防自循环刷屏。"""
        key = self._img_key(node_name, roi_tuple)
        last_map = getattr(self, "_last_dump", None)
        if last_map is None:
            last_map = self._last_dump = {}
        now = time.time()
        if now - last_map.get(key, 0.0) < _DUMP_MIN_INTERVAL:
            return
        last_map[key] = now

        saved = []
        for img, tag in ((work_bgr, "roi_crop"), (self._bool_to_bgr(red_mask), "red_mask")):
            p = self._save_debug_img(img, key, tag)
            if p:
                saved.append(p)
        if inner_best is not None:
            p = self._save_debug_img(self._bool_to_bgr(inner_best), key, "inner")
            if p:
                saved.append(p)
        if saved:
            print(f"[RedDotDetector] 失败截图 -> {saved}")  # 仅入 txt 日志，不上 UI
