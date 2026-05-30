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
# 在任意背景下稳健识别游戏 UI 中的红色通知点（内含白色感叹号结构）。
# 不依赖 TemplateMatch，通过颜色拓扑 + 垂直投影分析确认感叹号结构。
#
# [工作流程]
# 1. 按 argv.roi 裁剪工作区域（与任务级 roi 字段直接对应）。
# 2. HSV 双段 OR 过滤红色区域，得到红色连通域列表。
# 3. 按面积筛选候选红色 blob（过滤噪点和大面积红色 UI 元素）。
# 4. 对每个候选 blob，提取其包围框内的"内部亮色像素"（非红且亮）。
# 5. 对内部亮色区域做垂直投影，检测感叹号特有的"竖线-断层-圆点"双段结构。
# 6. 通过所有检查的第一个 blob 即命中，返回其在全图坐标系中的包围框。
#
# [为何此方案稳健]
# - 红色区域连片不影响：感叹号白色仍被红色包围，拓扑关系不变。
# - 任意背景：仅分析红色 blob 内部，外部背景完全隔离。
# - 尺寸变化：无模板匹配，面积阈值范围宽松即可适配多种大小。
# - 高光点误识别：高光点无垂直断层，双段检测可过滤。
#
# [参数说明]
#   hsv_ranges   (list)  红色 HSV 范围，默认覆盖标准红色双段（H 绕 0 点）。
#                        格式：[{lower:[H,S,V], upper:[H,S,V]}, ...]
#   red_area     (list)  红色 blob 的像素面积范围 [min, max]，默认 [30, 1200]。
#                        过小：噪点；过大：非红点的大面积红色 UI 元素。
#   inner_v_min  (int)   内部亮色像素的亮度下限 V（0-255），默认 50。
#                        用于过滤被红圈包围区域内的暗像素（抗锯齿边缘、渲染阴影）。
#                        背景漏入由 BFS 拓扑封闭检测负责，此参数无需考虑背景亮度。
#                        感叹号底部圆点可能很暗（游戏渲染压缩），不应设得过高。
#                        一般 30-80 即可；截图清晰且背景极暗时可低至 20。
#   inner_s_max  (int)   内部像素的最高饱和度 S（0-255），默认 90。
#                        感叹号是近白色（S 极低），设 90 可排除红点内部彩色/偏红像素。
#   gap_ratio    (float) 垂直投影断层深度阈值（0-1），默认 0.35。
#                        断层行像素数 / 最大行像素数 < gap_ratio 才认为有断层。
#                        越小越严格，适合断层明显的大红点；偏大适合小红点。
#   preset       (str)   预设节点名称（预设模式，见下方说明）。
#   debug        (bool)  默认 false。保存 ROI 裁剪图、红色掩膜、内部亮色掩膜。
#                        注：调用节点指定 preset 时，debug 参数在预设节点内控制，
#                        调用节点处的 debug 字段无效（预设模式直接委托执行）。
#
# [HSV 坐标系]
#   与 HSVShapeMatching 一致：OpenCV 标准（H 0-180，S/V 0-255）。
#
# ================================================================
#
# [使用示例 A] 独立模式 —— 单节点，自带完整参数
#
# {
#     "CheckRedDot": {
#         "recognition": "Custom",
#         "custom_recognition": "RedDotDetector",
#         "custom_recognition_param": {
#             "hsv_ranges": [
#                 {"lower": [0,   140, 120], "upper": [12,  255, 255]},
#                 {"lower": [165, 140, 120], "upper": [180, 255, 255]}
#             ],
#             "red_area":    [30, 1200],
#             "inner_v_min": 50,
#             "inner_s_max": 90,
#             "gap_ratio":   0.35,
#             "debug":       false
#         },
#         "roi": [950, 100, 40, 600],
#         "action": "Click",
#         "next": ["NextTask"]
#     }
# }
#
# [使用示例 B] 预设模式 —— 静态参数集中定义，调用节点只写 roi
#
#   适合场景：多个画板/按钮的右上角都需要检测红点，仅 roi 不同。
#   预设节点本身也是合法的独立模式节点（可直接运行）。
#   多包 override 时只需 override 预设节点，所有调用节点自动生效。
#
# {
#     "RedDot_Preset": {
#         "recognition": "Custom",
#         "custom_recognition": "RedDotDetector",
#         "custom_recognition_param": {
#             "hsv_ranges": [
#                 {"lower": [0,   140, 120], "upper": [12,  255, 255]},
#                 {"lower": [165, 140, 120], "upper": [180, 255, 255]}
#             ],
#             "red_area":    [30, 1200],
#             "inner_v_min": 50,
#             "inner_s_max": 90,
#             "gap_ratio":   0.35
#         }
#     },
#
#     "CheckPanel_A": {
#         "recognition": "Custom",
#         "custom_recognition": "RedDotDetector",
#         "custom_recognition_param": { "preset": "RedDot_Preset" },
#         "roi": [950, 100, 40, 200],
#         "action": "Click",
#         "next": ["AfterClick"]
#     },
#
#     "CheckPanel_B": {
#         "recognition": "Custom",
#         "custom_recognition": "RedDotDetector",
#         "custom_recognition_param": { "preset": "RedDot_Preset" },
#         "roi": [950, 300, 40, 200],
#         "action": "Click",
#         "next": ["AfterClick"]
#     }
# }
#
# ================================================================
# [调参指南] 5 阶段识别原理与逐步排查
# ================================================================
#
# 内部同时维护两条独立数据线，共用同一份原始截图：
#
#   work_bgr（roi_crop 原始像素）
#       │
#       ├─→ hsv_np      原始 HSV 数值，全程保留，阶段 4 直接读它
#       │       └─→ red_mask  bool 数组（True = 红色像素），供阶段 3/4 用
#       │
#       └─→ debug 图是把这两条数据可视化后存盘，不参与后续计算
#
# ────────────────────────────────────────────────────────────────
# 阶段 1  ROI 裁剪
# ────────────────────────────────────────────────────────────────
# 参数：任务 JSON 的顶层 roi 字段（不在 custom_recognition_param 里）
# 输出：debug/rdd_*_roi_crop.png  ← 代码实际处理的像素区域
#
# 排查：打开 roi_crop.png，红点必须完整在图内。
#       若图里没有红点 → 坐标填错了，对着游戏原图重新量取。
#       建议 roi 比红点略大 2-4px，给菱形边缘留余量。
#
# ────────────────────────────────────────────────────────────────
# 阶段 2  HSV 过滤，生成红色掩膜
# ────────────────────────────────────────────────────────────────
# 参数：hsv_ranges（H 0-180 / S 0-255 / V 0-255，OpenCV 坐标系）
# 输出：debug/rdd_*_red_mask.png  ← 黑=检测为红，白=非红
#
# 调参方法：
#   在游戏截图上用取色工具拾取红点几个像素（建议取菱形边缘 3-4 点），
#   记录 RGB → 转 HSV。H/S/V 各取最小值作下限，最大值作上限，
#   再各自留 10-20 的余量。
#
#   注意：游戏红色常常跨越 H=0（如 H 在 170-180 和 0-10 各有一段），
#   必须拆成两组 hsv_ranges OR 合并，单组 lower > upper 无效。
#
#   红色通常：H 0-12 或 165-180，S > 130，V > 100。
#   偏橙红（H 偏大）：适当调高上限。
#   暗红（V 低）：适当调低 V 下限。
#
#   红色连片（红点与其他红色 UI 连成一块）不影响后续识别：
#   感叹号被红色从四面包围的拓扑关系不变。
#
# 判断：
#   red_mask 全白 → hsv_ranges 没覆盖到实际红色，收窄 S/V 下限
#   大片黑色（背景也黑了）→ hsv_ranges 太宽，提高 S 或 V 下限
#   菱形轮廓完整黑色 → 正常，进入下一阶段
#
# ────────────────────────────────────────────────────────────────
# 阶段 3  连通域面积筛选
# ────────────────────────────────────────────────────────────────
# 参数：red_area [min, max]
# 输出（有 blob 通过时）：debug/rdd_*_inner_blob{N}.png 被创建
#
# 调参方法：
#   先不看 inner_blob，看 inner_blob 文件有没有被生成：
#     无文件 → 所有 blob 都被 red_area 过滤掉了
#     有文件 → 至少一个 blob 通过，继续看阶段 4
#
#   面积估算：菱形面积 ≈ 对角线² / 2。16px 菱形 ≈ 128px，
#   10px 菱形 ≈ 50px。菱形外圈环（若内部非红）更小，建议 min 设 30。
#   max 设 1200 通常够用；若场景有大红色 UI 区域，适当调小。
#
# ────────────────────────────────────────────────────────────────
# 阶段 4  内部亮色像素提取
# ────────────────────────────────────────────────────────────────
# 参数：inner_v_min、inner_s_max
# 数据来源：hsv_np（原始 HSV，不是黑白 red_mask）
# 输出：debug/rdd_*_inner_blob{N}.png  ← 黑=通过的内部像素，白=被排除
#
# 原理：
#   分两步提取真正被红圈包围的亮色像素：
#   ① BFS 拓扑封闭检测（背景隔离）
#        对包围框内的非红像素做连通域标注，凡是能从矩形边框触达的连通域
#        视为"外侧背景"，剩余非红像素才是被红圈真正封闭的内部区域（enclosed）。
#        此步骤无关背景亮度，彻底消除矩形包围框四角背景像素的漏入问题。
#   ② 亮色过滤（排除内部暗像素）
#        在 enclosed 区域内进一步筛选：
#        V >= inner_v_min → 排除抗锯齿边缘和渲染阴影（暗像素）
#        S <= inner_s_max → 排除偏彩色/偏红的内部渲染像素
#
#   游戏渲染会压缩小尺寸元素的亮度，感叹号底部圆点可能只有 V=60-80。
#
# 调参方法：
#   在游戏截图上拾取感叹号内部像素的 RGB（取竖线中段 + 底部圆点各几点），
#   转 HSV，记录 V 最小值和 S 最大值。
#   inner_v_min 设为：min(感叹号各点V) - 10（无需考虑背景 V）。
#   inner_s_max 设为：max(感叹号各点S) + 10，通常 < 90 就够。
#
# 判断：
#   inner_blob 几乎全黑（整个包围框都黑）→ inner_v_min 太低或 inner_s_max 太高
#   inner_blob 只有 1-3 个黑点 → inner_v_min 偏高，感叹号暗区被截断
#   inner_blob 竖线+圆点都是黑色（两段清晰）→ 最佳状态，进入阶段 5
#   inner_blob 只有竖线顶端（1列最亮像素）→ inner_v_min 偏高但尚可，
#     若阶段 5 也能通过则无需调整（1列已足够形成双段投影）
#
# ────────────────────────────────────────────────────────────────
# 阶段 5  垂直投影双段检测（感叹号结构验证）
# ────────────────────────────────────────────────────────────────
# 参数：gap_ratio（默认 0.35）
# 输出：debug=true 时控制台打印投影数组，例如：
#   [RedDotDetector] blob1 inner_bright 垂直投影: [0, 0, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
#
# 原理：
#   对 inner_bright 逐行求和（每行有几个亮色像素），得到垂直投影。
#   算法先裁掉头尾的零行（包围框上下的黑色背景），在有效区段内
#   找最小值行（= 竖线与圆点之间那 1px 红色间隔），验证：
#     gap行像素数 / 最高行像素数 < gap_ratio（断层足够深）
#     断层上方至少 2 行有像素（竖线部分，防止游离单行噪点误判）
#     断层下方至少 1 行有像素（圆点部分，兼容极小红点）
#   三条全通过 → 命中。
#
#   实际案例（16px 红点，感叹号竖线 2px 宽 × 3px 高，圆点 1px）：
#     投影 [0,0,0, 1,1,1, 0, 1, 0,0,0,0]
#                  ↑↑↑  gap  ↑
#                  竖线       圆点
#   每行只有 1 个像素（只有最亮的左列通过 inner_v_min）仍能正确命中。
#   多几列像素不会更准，少几列也不会失败，只要双段结构存在。
#
# 调参方法：
#   先看投影日志，确认数组里有"非零-零-非零"的模式。
#   若有此模式但仍失败 → gap_ratio 太小，将 0 那行的值除以最大值，
#     结果即为所需最小 gap_ratio（再加 0.05 余量）。
#   若投影是单峰（无零行）→ 感叹号像素太少或 inner_v_min 还需再降。
#
# ────────────────────────────────────────────────────────────────
# 一句话调参口诀
# ────────────────────────────────────────────────────────────────
# roi_crop 没红点    → 改 roi 坐标
# red_mask 全白      → 降 hsv_ranges 的 S/V 下限
# 没有 inner_blob 文件 → 降 red_area min（或升 max）
# inner_blob 只有 1-2 点 → 降 inner_v_min（可低至 20-30）
# 投影日志无零行     → 继续降 inner_v_min；或检查红圈是否有缺口导致 BFS 漏入
# 投影有零行仍失败   → 升 gap_ratio（0.4~0.5）
#
# ================================================================

_RED_RANGES_DEFAULT = [
    {"lower": [0,   130, 100], "upper": [12,  255, 255]},
    {"lower": [165, 130, 100], "upper": [180, 255, 255]},
]


@AgentServer.custom_recognition("RedDotDetector")
class RedDotDetector(CustomRecognition):
    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        """
        红点感叹号识别器。参数格式与完整示例见模块顶部注释块。
        """
        try:
            raw = argv.custom_recognition_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))

            if "preset" in params:
                return self._run_preset(context, argv, params)
            return self._run_standalone(argv, params)

        except Exception:
            mfaalog.error(f"[RedDotDetector] 执行异常:\n{traceback.format_exc()}")
            return None

    # ------------------------------------------------------------------
    # 预设模式
    # ------------------------------------------------------------------

    def _run_preset(self, context: Context,
                    argv: CustomRecognition.AnalyzeArg, params: dict):
        """
        预设模式：将 argv.roi 裁剪后交给预设节点执行，结果坐标加回偏移。
        调用节点除 preset 外的其他参数会被忽略；参数覆盖请直接 override 预设节点。
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

        detail = context.run_recognition(preset_node, cropped)
        if detail and detail.hit:
            bx, by, bw, bh = detail.box
            adjusted = (bx + rx, by + ry, bw, bh)
            print(f"[RedDotDetector] [preset:{preset_node}] 命中 → {adjusted}")
            return CustomRecognition.AnalyzeResult(box=adjusted, detail={"preset": preset_node})
        return None

    # ------------------------------------------------------------------
    # 独立模式
    # ------------------------------------------------------------------

    def _run_standalone(self, argv: CustomRecognition.AnalyzeArg, params: dict):
        """独立模式：完整执行 HSV 过滤 → blob 筛选 → 感叹号结构检测。"""
        hsv_ranges  = params.get("hsv_ranges", _RED_RANGES_DEFAULT)
        area_min, area_max = params.get("red_area", [30, 1200])
        inner_v_min = params.get("inner_v_min", 50)
        inner_s_max = params.get("inner_s_max", 90)
        gap_ratio   = params.get("gap_ratio",   0.35)
        debug       = params.get("debug", False)

        # 1. 按 roi 裁剪工作区域
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

        # 2. BGR → PIL → HSV
        pil_img = Image.fromarray(work_bgr[..., ::-1])
        hsv_np  = np.array(pil_img.convert("HSV"))

        # 3. 红色 HSV 掩膜
        red_mask = _compute_hsv_mask(hsv_np, hsv_ranges)

        ts = f"{time.time():.3f}".replace('.', '_')
        if debug:
            self._save_debug_img(work_bgr, "roi_crop", ts)
            self._save_debug_img(self._bool_to_bgr(red_mask), "red_mask", ts)

        # 4. 连通域标注
        labeled, n_blobs = _label_blobs(red_mask)

        # 5. 逐 blob 检测
        for i in range(1, n_blobs + 1):
            blob = (labeled == i)
            area = int(np.sum(blob))
            if not (area_min <= area <= area_max):
                continue

            # blob 包围框
            rows = np.where(np.any(blob, axis=1))[0]
            cols = np.where(np.any(blob, axis=0))[0]
            bx0, bx1 = int(cols[0]), int(cols[-1])
            by0, by1 = int(rows[0]), int(rows[-1])
            bw, bh = bx1 - bx0 + 1, by1 - by0 + 1

            # 包围框内的非红亮色像素（感叹号区域）
            box_red = red_mask[by0:by1 + 1, bx0:bx1 + 1]
            box_hsv = hsv_np  [by0:by1 + 1, bx0:bx1 + 1]

            # 拓扑封闭过滤：只保留被红色像素真正包围的非红区域，排除矩形四角的背景漏洞
            # 对包围框内非红像素做连通域标注，凡是能从矩形边框触达的连通域 = 外侧背景
            non_red_crop = ~box_red
            labeled_crop, _ = _label_blobs(non_red_crop)
            border_labels = (
                set(labeled_crop[0, :].tolist())
                | set(labeled_crop[-1, :].tolist())
                | set(labeled_crop[:, 0].tolist())
                | set(labeled_crop[:, -1].tolist())
            )
            border_labels.discard(0)  # 0 是红色像素占位，不是连通域
            enclosed = non_red_crop & ~np.isin(labeled_crop, list(border_labels))

            inner_bright = (
                enclosed
                & (box_hsv[:, :, 2] >= inner_v_min)
                & (box_hsv[:, :, 1] <= inner_s_max)
            )

            if not np.any(inner_bright):
                continue

            if debug:
                self._save_debug_img(self._bool_to_bgr(inner_bright), f"inner_blob{i}", ts)
                proj_str = np.sum(inner_bright, axis=1).astype(int).tolist()
                mfaalog.info(f"[RedDotDetector] blob{i} inner_bright 垂直投影: {proj_str}")

            # 感叹号垂直双段检测
            if not self._has_exclamation(inner_bright, gap_ratio, debug):
                mfaalog.debug(f"[RedDotDetector] blob{i} gap 检测未通过")
                continue

            result_box = (bx0 + rx, by0 + ry, bw, bh)
            print(f"[RedDotDetector] 命中: box={result_box}, red_area={area}")
            return CustomRecognition.AnalyzeResult(
                box=result_box,
                detail={"red_area": area, "box": list(result_box)},
            )

        return None

    # ------------------------------------------------------------------
    # 感叹号结构检测
    # ------------------------------------------------------------------

    def _has_exclamation(self, inner_bright: np.ndarray, gap_ratio: float, debug: bool = False) -> bool:
        """
        检测内部亮色区域是否呈"竖线-断层-圆点"的垂直双段结构（感叹号）。

        原理：感叹号竖线与圆点之间有 1px 红色分隔，垂直投影在该行像素数为 0
        或极少；圆形高光点无此断层，长宽比接近 1:1 也会被过滤。

        关键设计：先裁去投影头尾的空白行再找断层，避免 argmin 落到红点包围框
        顶部/底部的背景行上（这些行对应黑色背景，inner_bright 恒为 0）。
        双段存在性要求竖线段（断层上方）至少 2 行非零、圆点段（断层下方）至少 1 行非零，
        不设最小像素数，兼容底部圆点极小的情况。
        """
        proj = np.sum(inner_bright, axis=1).astype(np.float32)  # 每行亮色像素数
        total = int(proj.sum())

        if total < 3:
            return False

        # 找到有非零投影的行范围，去掉包围框上下的空白行
        nonzero_rows = np.where(proj > 0)[0]
        if len(nonzero_rows) < 2:
            # 只有 0 或 1 行有像素，无法形成双段
            return False

        first_nz = int(nonzero_rows[0])
        last_nz  = int(nonzero_rows[-1])
        trimmed  = proj[first_nz:last_nz + 1]  # 仅有效数据区段

        if len(trimmed) < 3:
            # 区段太短，感叹号竖线+间距+圆点至少需要 3 行；以长宽比兜底
            ph, pw = inner_bright.shape
            return ph > pw * 1.3

        # 在有效区段内部（去掉首尾）寻找最小投影行（断层位置）
        inner_trimmed = trimmed[1:-1]
        gap_rel = int(np.argmin(inner_trimmed))          # 相对于 trimmed[1:-1]
        gap_abs = first_nz + gap_rel + 1                  # 映射回原始 proj 下标

        peak = proj.max()
        if peak == 0:
            return False

        # 断层深度检查：断层行必须显著低于峰值行
        actual_ratio = proj[gap_abs] / peak
        if debug:
            mfaalog.info(
                f"[RedDotDetector] gap_ratio 检查: 断层行[row {gap_abs + 1}]={int(proj[gap_abs])}px"
                f" / 峰值={int(peak)}px = {actual_ratio:.3f}"
                f" (阈值 gap_ratio={gap_ratio})"
                f" → {'通过' if actual_ratio <= gap_ratio else '截断'}"
            )
        if actual_ratio > gap_ratio:
            return False

        # 双段存在性：竖线段（断层上方）至少 2 行非零，圆点段（断层下方）至少 1 行
        # 竖线要求 ≥2 行，防止单个游离像素把"噪点+大块"误判为感叹号双段结构
        # 圆点允许 1 行，兼容底部圆点像素极少的小尺寸红点
        above_nz = int(np.sum(proj[first_nz:gap_abs] > 0))
        has_below = bool(np.any(proj[gap_abs + 1:last_nz + 1] > 0))
        if debug:
            mfaalog.info(
                f"[RedDotDetector] 双段检查: 竖线段 {above_nz} 行非零 (需≥2)"
                f", 圆点段 {'有' if has_below else '无'}像素"
                f" → {'通过' if above_nz >= 2 and has_below else '截断'}"
            )
        return above_nz >= 2 and has_below

    # ------------------------------------------------------------------
    # 调试辅助
    # ------------------------------------------------------------------

    def _bool_to_bgr(self, mask: np.ndarray) -> np.ndarray:
        """bool mask → 白底黑形状 BGR 图。"""
        out = np.full((*mask.shape, 3), 255, dtype=np.uint8)
        out[mask] = [0, 0, 0]
        return out

    def _save_debug_img(self, bgr_img: np.ndarray, tag: str, ts: str) -> None:
        try:
            debug_dir = "debug_images"
            os.makedirs(debug_dir, exist_ok=True)
            path = f"{debug_dir}/rdd_{ts}_{tag}.png"
            Image.fromarray(bgr_img[..., ::-1]).save(path)
            mfaalog.info(f"[RedDotDetector] 调试图 → {path}")
        except Exception as e:
            mfaalog.warning(f"[RedDotDetector] 调试图保存失败: {e}")
