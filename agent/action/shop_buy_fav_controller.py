"""
商店购买 V3 - 单页收藏对齐动作

职责边界：已进入某卡带商店页面后，对当前页执行收藏对齐。

识别策略：
    - 商品名: OCR（Pipeline 节点定义 ROI）
    - 星星位置: TemplateMatch method 5（颜色不敏感，黄灰都命中）
    - 星星颜色: numpy 对星星完整 box 采样，按高饱和度像素占比判色
      （黄星尖角高饱和 ~68%，灰星 ~0%，规避白色中心干扰）

数据来源（全部从 Pipeline 节点读取，Python 零硬编码）：
    - 当前卡带名     ← custom_action_param（经 json.loads 解包）
    - 购物清单       ← ShopBuy_Data.attach[卡带名]
    - OCR 过滤词     ← ShopBuy_Data.attach["ocr_exclude"]
    - 商品名         ← ShopBuy_ReadNames_OCR 节点
    - 星星位置       ← ShopBuy_Star_All 节点
"""

import json
import re
import time
import numpy as np
from maa.custom_action import CustomAction
from maa.context import Context
from maa.agent.agent_server import AgentServer
from utils import mfaalog


# 数据节点名
DATA_NODE = "Arbitrage_ShopBuy_Data"

# 识别节点名
NODE_OCR  = "Arbitrage_ShopBuy_ReadNames_OCR"
NODE_STAR = "Arbitrage_ShopBuy_Star_All"

# 颜色判定参数
# 黄星尖角像素饱和度 >0.3 占比约 68%，灰星约 0%
# 阈值设 15%，远低于黄星、远高于灰星，留足余量
SAT_PIXEL_THRESHOLD = 0.3   # 单像素饱和度阈值
SAT_RATIO_THRESHOLD = 0.15  # 高饱和像素占比阈值

# 配对参数（星星右边缘 → 商品名左边缘）
BIND_DX_MIN = 5    # 商品名至少在星星右侧 5px
BIND_DX_MAX = 40   # 最远不超过 40px
BIND_DY_MAX = 15   # Y 轴差距不超过 15px

# 商品名最大长度（中文字符数），过滤掉 Toast 消息
NAME_MAX_LEN = 5

# 点击间延迟（秒），等待菱形光特效消散
CLICK_DELAY = 1.5
# 验证前等待（秒），等待 Toast 消息淡出
VERIFY_DELAY = 2.0


@AgentServer.custom_action("ShopBuyFavController")
class ShopBuyFavController(CustomAction):

    # ==========================================
    # 主入口
    # ==========================================
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        cart_name = argv.custom_action_param
        if isinstance(cart_name, str):
            try:
                cart_name = json.loads(cart_name)
            except (json.JSONDecodeError, TypeError):
                pass

        if not cart_name or not isinstance(cart_name, str):
            mfaalog.warning("[ShopBuy] ⚠️ 未收到卡带名，中止。")
            return False

        mfaalog.info(f"[ShopBuy] 🛒 收藏对齐启动 → 卡带 [{cart_name}]")

        target_items, ocr_exclude = self._load_config(context, cart_name)
        if target_items is None:
            return False

        mfaalog.info(
            f"[ShopBuy] 📋 [{cart_name}] 目标商品 ({len(target_items)}项): "
            f"{', '.join(target_items)}"
        )

        return self._align_favorites(
            context, target_items, ocr_exclude, cart_name
        )

    # ==========================================
    # 配置读取
    # ==========================================
    def _load_config(self, context: Context, cart_name: str):
        node_obj = context.get_node_object(DATA_NODE)
        if not node_obj or not getattr(node_obj, 'attach', None):
            mfaalog.warning(
                f"[ShopBuy] ❌ 无法读取 [{DATA_NODE}] 的 attach。"
                f"提示：enabled:false 的节点可能无法访问。"
            )
            return None, None

        attach = node_obj.attach

        items_str = attach.get(cart_name)
        if not items_str or not isinstance(items_str, str):
            mfaalog.warning(
                f"[ShopBuy] ⚠️ 未找到卡带 [{cart_name}] 的购物清单。"
            )
            return None, None

        target_items = self._parse_item_list(items_str)
        if not target_items:
            mfaalog.warning(f"[ShopBuy] ⚠️ [{cart_name}] 购物清单解析为空。")
            return None, None

        exclude_str = attach.get("ocr_exclude", "")
        ocr_exclude = (
            self._parse_item_list(exclude_str) if exclude_str else set()
        )
        mfaalog.info(
            f"[ShopBuy] 🔧 ocr_exclude ({len(ocr_exclude)}项): "
            f"{ocr_exclude if ocr_exclude else '空！'}"
        )

        return target_items, ocr_exclude

    def _parse_item_list(self, raw_str: str) -> set:
        raw_items = [
            x.strip()
            for x in re.split(r'[，,;|]+', raw_str)
            if x.strip()
        ]
        cleaned = set()
        for item in raw_items:
            c = re.sub(r'[^\w\u4e00-\u9fa5]', '', item)
            if c:
                cleaned.add(c)
        return cleaned

    # ==========================================
    # 单页对齐
    # ==========================================
    def _align_favorites(
        self, context, target_items, ocr_exclude, cart_name,
        max_retries=1
    ) -> bool:
        for attempt in range(1 + max_retries):
            if context.tasker.stopping:
                return False

            label = "初次" if attempt == 0 else f"重试第{attempt}次"
            mfaalog.info(f"[ShopBuy] 🔍 [{cart_name}] {label}扫描...")

            screenshot = (
                context.tasker.controller.post_screencap().wait().get()
            )
            if screenshot is None:
                mfaalog.warning("[ShopBuy] ❌ 截图失败。")
                return False

            entities = self._scan_page(context, screenshot, ocr_exclude)
            if entities is None:
                mfaalog.warning(f"[ShopBuy] ⚠️ [{cart_name}] 识别失败。")
                return False

            actions = self._decide_actions(entities, target_items)
            if not actions:
                mfaalog.info(
                    f"[ShopBuy] ✨ [{cart_name}] 收藏状态已正确，无需操作。"
                )
                return True

            if attempt > 0:
                mfaalog.warning(
                    f"[ShopBuy] ⚠️ [{cart_name}] "
                    f"仍有 {len(actions)} 项未对齐，重试..."
                )

            self._execute_clicks(context, actions, cart_name)

            if attempt < max_retries:
                mfaalog.info(
                    f"[ShopBuy] 🔁 [{cart_name}] "
                    f"等待 {VERIFY_DELAY}s 后验证..."
                )
                time.sleep(VERIFY_DELAY)

        # 最终验证
        mfaalog.info(f"[ShopBuy] 🔎 [{cart_name}] 最终验证...")
        time.sleep(VERIFY_DELAY)

        final_ss = context.tasker.controller.post_screencap().wait().get()
        if final_ss is None:
            return False
        final_entities = self._scan_page(context, final_ss, ocr_exclude)
        if final_entities is None:
            return False
        final_actions = self._decide_actions(final_entities, target_items)
        if final_actions:
            mfaalog.warning(
                f"[ShopBuy] ❌ [{cart_name}] 最终验证仍有 "
                f"{len(final_actions)} 项未对齐: "
                + ", ".join(
                    f"{a['name']}({'需点亮' if a['action']=='light' else '需熄灭'})"
                    for a in final_actions
                )
            )
            return False

        mfaalog.info(f"[ShopBuy] ✅ [{cart_name}] 收藏对齐验证通过！")
        return True

    # ==========================================
    # 页面扫描
    # ==========================================
    def _scan_page(self, context, screenshot, ocr_exclude):

        # --- OCR 商品名 ---
        ocr_result = context.run_recognition(NODE_OCR, screenshot)
        if not ocr_result or not ocr_result.all_results:
            mfaalog.warning("[ShopBuy] OCR 未识别到任何文本。")
            return None

        name_items = []
        for match in ocr_result.all_results:
            box = getattr(match, 'box', None)
            text = getattr(match, 'text', None)
            if box is None or text is None:
                continue
            x, y, w, h = box
            cleaned = re.sub(r'[^\w\u4e00-\u9fa5]', '', text)
            if not cleaned or cleaned.isdigit():
                continue
            if cleaned in ocr_exclude:
                continue
            # 过滤 Toast 消息（"已将商品蘑菇加入收藏"等长文本）
            if len(cleaned) > NAME_MAX_LEN:
                continue
            name_items.append({
                "name": cleaned,
                "left_x": x,
                "cy": y + h / 2,
            })

        mfaalog.info(f"[ShopBuy] OCR 过滤后: {len(name_items)} 项")
        for item in name_items:
            mfaalog.info(
                f"  {item['name']:6s} "
                f"left_x={item['left_x']:.0f} cy={item['cy']:.0f}"
            )

        if not name_items:
            mfaalog.warning("[ShopBuy] OCR 清洗后无有效商品名。")
            return None

        # --- 星星位置 ---
        star_result = context.run_recognition(NODE_STAR, screenshot)
        if not star_result or not star_result.filtered_results:
            mfaalog.warning("[ShopBuy] 未识别到任何星星。")
            return None

        img = np.asarray(screenshot)
        img_h, img_w = img.shape[:2]

        all_stars = []
        for match in star_result.filtered_results:
            box = getattr(match, 'box', None)
            if box is None:
                continue
            bx, by, bw, bh = box
            color = self._classify_star_color(
                img, bx, by, bw, bh, img_w, img_h
            )
            all_stars.append({
                "box": [bx, by, bw, bh],
                "cx": bx + bw / 2,
                "cy": by + bh / 2,
                "right_x": bx + bw,
                "color": color,
            })

        yellow_n = sum(1 for s in all_stars if s["color"] == "yellow")
        gray_n = len(all_stars) - yellow_n
        mfaalog.info(
            f"[ShopBuy] 星星: {len(all_stars)} 个 "
            f"({yellow_n} 黄, {gray_n} 灰)"
        )

        if not all_stars:
            return None

        # --- 配对 ---
        return self._bind_star_to_name(all_stars, name_items)

    # ==========================================
    # 星星颜色判定（全 box + 高饱和像素占比）
    # ==========================================
    def _classify_star_color(
        self, img, bx, by, bw, bh, img_w, img_h
    ) -> str:
        """
        采样星星完整 box，计算饱和度 >0.3 的像素占比。
        黄星 ~68%，灰星 ~0%。阈值 15% 可靠分离。
        """
        x1 = max(0, bx)
        y1 = max(0, by)
        x2 = min(img_w, bx + bw)
        y2 = min(img_h, by + bh)

        patch = img[y1:y2, x1:x2, :3].astype(np.float32)
        if patch.size == 0:
            return "gray"

        max_ch = patch.max(axis=2)
        min_ch = patch.min(axis=2)
        safe_max = np.where(max_ch > 0, max_ch, 1.0)
        saturation = (max_ch - min_ch) / safe_max

        high_sat_ratio = float((saturation > SAT_PIXEL_THRESHOLD).mean())

        result = "yellow" if high_sat_ratio > SAT_RATIO_THRESHOLD else "gray"
        mfaalog.info(
            f"[ShopBuy]   🎨 ({bx},{by}) "
            f"high_sat={high_sat_ratio:.0%} → {result}"
        )
        return result

    # ==========================================
    # 星星→商品名 配对
    # ==========================================
    def _bind_star_to_name(self, all_stars, name_items):
        entities = []
        used_names = set()

        for star in all_stars:
            best_name = None
            best_dx = float('inf')

            for i, name_item in enumerate(name_items):
                if i in used_names:
                    continue
                dx = name_item["left_x"] - star["right_x"]
                dy = abs(name_item["cy"] - star["cy"])
                if BIND_DX_MIN <= dx <= BIND_DX_MAX and dy <= BIND_DY_MAX:
                    if dx < best_dx:
                        best_dx = dx
                        best_name = (i, name_item)

            if best_name:
                idx, name_item = best_name
                used_names.add(idx)
                entities.append({
                    "name": name_item["name"],
                    "star_color": star["color"],
                    "star_cx": star["cx"],
                    "star_cy": star["cy"],
                })
                mfaalog.info(
                    f"[ShopBuy]   🔗 [{name_item['name']}] "
                    f"↔ 星({star['right_x']:.0f},{star['cy']:.0f}) "
                    f"dx={best_dx:.0f} {star['color']}"
                )
            else:
                mfaalog.warning(
                    f"[ShopBuy] ⚠️ 星星 box={star['box']} 未配对到商品名"
                )

        return entities

    # ==========================================
    # 四分类决策
    # ==========================================
    def _decide_actions(self, entities, target_items):
        actions = []
        for entity in entities:
            name = entity["name"]
            color = entity["star_color"]
            is_target = name in target_items

            if is_target and color == "gray":
                actions.append({
                    "name": name, "action": "light",
                    "star_cx": entity["star_cx"],
                    "star_cy": entity["star_cy"],
                })
                mfaalog.info(f"[ShopBuy]   ⭐ [{name}] 目标+灰星 → 将点亮")
            elif not is_target and color == "yellow":
                actions.append({
                    "name": name, "action": "extinguish",
                    "star_cx": entity["star_cx"],
                    "star_cy": entity["star_cy"],
                })
                mfaalog.info(f"[ShopBuy]   🔄 [{name}] 非目标+黄星 → 将熄灭")
            elif is_target and color == "yellow":
                mfaalog.info(f"[ShopBuy]   ✓  [{name}] 目标+黄星 → 已正确")

        return actions

    # ==========================================
    # 执行点击
    # ==========================================
    def _execute_clicks(self, context, actions, cart_name):
        actions.sort(key=lambda a: (a["star_cy"], a["star_cx"]))
        mfaalog.info(
            f"[ShopBuy] 🎯 [{cart_name}] "
            f"共 {len(actions)} 个星星待点击..."
        )

        for i, act in enumerate(actions, 1):
            if context.tasker.stopping:
                break
            cx = int(act["star_cx"])
            cy = int(act["star_cy"])
            verb = "点亮" if act["action"] == "light" else "熄灭"
            mfaalog.info(
                f"[ShopBuy]   👆 {i}/{len(actions)} "
                f"{verb} [{act['name']}] @ ({cx}, {cy})"
            )
            context.tasker.controller.post_click(cx, cy).wait()
            time.sleep(CLICK_DELAY)
