import os
import json
import re
from maa.custom_action import CustomAction
from maa.context import Context
from maa.agent.agent_server import AgentServer
from utils import mfaalog

@AgentServer.custom_action("ArbitrageSellController")
class ArbitrageSellController(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        mfaalog.info("[Arbitrage] 🚀 商店套利-出售主控器启动")
        
        # ==========================================
        # 1. 提取并合并 Attach 白名单
        # ==========================================
        whitelist_set = set()
        
        # 假设我们将此动作绑定在 Arbitrage_ShopSell_Active 节点
        node_obj = context.get_node_object("Arbitrage_ShopSell_Active")
        
        if node_obj and node_obj.attach:
            # 遍历 attach 中的所有 key (default, Drops, 以及 UI 传进来的 SellName)
            for key, val_str in node_obj.attach.items():
                if isinstance(val_str, str) and val_str.strip():
                    # 按照逗号、分号、中文逗号切分
                    raw_items = [x.strip() for x in re.split(r'[，,;|]+', val_str) if x.strip()]
                    for item in raw_items:
                        # 使用和 OCR 底层一模一样的清洗规则，保证 100% 绝对匹配
                        cleaned_item = re.sub(r'[^\w\u4e00-\u9fa5]', '', item)
                        if cleaned_item:
                            whitelist_set.add(cleaned_item)
                    
        if not whitelist_set:
            mfaalog.warning("[Arbitrage] ⚠️ 未读取到任何待售物品白名单，流程结束。")
            return True
            
        mfaalog.info(f"[Arbitrage] 📋 期望售卖清单 ({len(whitelist_set)}项): {', '.join(whitelist_set)}")

        # ==========================================
        # 2. 扫描阶段：识别当前页 -> 翻页 -> 截断
        # ==========================================
        targets_to_sell = [] # 记录所有达标待售的商品
        all_max_price_items = []   # 记录所有扫描到的最高价商品（仅用于展示）
        page_count = 1
        
        while not context.tasker.stopping:
            mfaalog.info(f"[Arbitrage] 📷 正在扫描第 {page_count} 页价目表...")
            
            # 调用内部的 V8 图像解析引擎
            page_results = self._parse_current_page(context)
            if not page_results:
                mfaalog.warning("[Arbitrage] ⚠️ 识别失败或页面无商品，结束扫描。")
                break
                
            has_non_max = False
            for item in page_results:
                name = item["name"]
                is_max = item["is_max_price"]
                cart = item["target_cartridge"]
                
                # 触发截断：遇到非最高价商品
                if not is_max:
                    has_non_max = True
                    mfaalog.info(f"[Arbitrage] 🛑 扫描到非最高价商品 [{name}]，已触及利润边界，停止向下扫描。")
                    break 

                # 记录所有扫描到的最高价商品（去重保存）
                if name not in all_max_price_items:
                    all_max_price_items.append(name)
                   
                # 检查是否在白名单中
                if name in whitelist_set:
                    # 查重防抖 (防止翻页重叠导致同个物品被记录两次)
                    if not any(t["name"] == name for t in targets_to_sell):
                        targets_to_sell.append({
                            "name": name, 
                            "cartridge_raw": cart
                        })

            if has_non_max:
                break 
                
            # 翻页动作：调用你写好的精准滑动链
            mfaalog.info("[Arbitrage] ⏬ 下滑翻页...")
            # 注意：如果下面这个节点跑完了，价目表应该已经成功翻页
            swip_success = context.run_task("Arbitrage_Swip_PriceList") 
            if not swip_success:
                mfaalog.warning("[Arbitrage] ⚠️ 翻页任务执行失败或遇到异常，停止扫描。")
                break
                
            page_count += 1
        
        # 🌟 优化日志 2：列出今日市面上的所有最高价商品
        mfaalog.info(f"[Arbitrage] 📈 今日最高价商品总览: {', '.join(all_max_price_items) if all_max_price_items else '无'}")
        
        # ==========================================
        # 3. 派发阶段：循环注入并执行售卖节点链
        # ==========================================
        if not targets_to_sell:
            mfaalog.info("[Arbitrage] 💤 今日无符合条件的最高价商品，收工！")
            return True
            
        # 🌟 优化日志 3：列出最终交集的执行清单
        final_sell_names = [t["name"] for t in targets_to_sell]
        mfaalog.info(f"[Arbitrage] 🛒 扫描完毕！确认共 {len(targets_to_sell)} 项物品待出售: {', '.join(final_sell_names)}")
        
        for idx, target in enumerate(targets_to_sell, 1):
            if context.tasker.stopping: break
            
            item_name = target["name"]
            cart_raw = target["cartridge_raw"]
            mfaalog.info(f"[Arbitrage] 👉 正在执行 {idx}/{len(targets_to_sell)}: 前往 [{cart_raw}] 售卖 [{item_name}]")

            # 核心：构造多节点参数替换字典
            override_cfg = {
                "Arbitrage_Sell_PackShopSwich": {
                    "expected": cart_raw
                },
                "Arbitrage_Sell_Item_ListTraverse": {
                    "expected": item_name
                }
            }
            
            # 拉起 JSON 端的出售链，并阻塞等待它执行完毕
            # 起点设为进入出售菜单的识别节点
            sell_result = context.run_task("Arbitrage_Sell_HUB", pipeline_override=override_cfg)
            
            if sell_result:
                mfaalog.info(f"[Arbitrage] ✅ [{item_name}] 售卖流程执行成功！")
            else:
                mfaalog.warning(f"[Arbitrage] ❌ [{item_name}] 售卖流程中断或失败，继续尝试下一个。")
                
        mfaalog.info("[Arbitrage] 🎉 所有售卖派发任务执行结束！")
        return True

    # ==========================================
    # 附：V8 图像解析引擎 
    # ==========================================
    def _parse_current_page(self, context: Context) -> list:
        # 配置区
        EQUATOR_OFFSET = 7 
        COL_NAME_MIN, COL_NAME_MAX = 470, 730
        COL_PRICE_MIN, COL_PRICE_MAX = 880, 960
        COL_CART_MIN = 960

        screenshot = context.tasker.controller.post_screencap().wait().get()
        # 新增的防御逻辑：如果截图失败，记录警告并安全退出当前解析
        if screenshot is None:
            print("[Arbitrage] ❌ 严重错误: 底层截图获取失败 (返回 None)！跳过当前页解析。")
            return []
        reco_result = context.run_recognition("Arbitrage_Sell_ReadList_OCR", screenshot)
        
        if not reco_result or not reco_result.hit or not reco_result.all_results:
            return []
            
        all_texts = []
        for match in reco_result.all_results:
            # 消除编辑器告警 + 防御性编程：确保当前结果确实包含所需属性
            box = getattr(match, 'box', None)
            text = getattr(match, 'text', None)
            
            # 如果没有这两个属性，直接跳过
            if box is None or text is None:
                continue

            x, y, w, h = box
            all_texts.append({
                "box": box, "text": text,
                "cx": x + w / 2, "cy": y + h / 2, "bottom_y": y + h
            })
        
        anchors = []
        for t in all_texts:
            if COL_NAME_MIN <= t["cx"] < COL_NAME_MAX:
                cleaned = re.sub(r'[^\w\u4e00-\u9fa5]', '', t["text"])
                if cleaned and not cleaned.isdigit():
                    if not any(abs(t["cy"] - a['anchor_cy']) < 30 for a in anchors):
                        anchors.append({
                            'name': cleaned, 'anchor_cy': t["cy"],
                            'equator_y': t["bottom_y"] + EQUATOR_OFFSET, 'items': []
                        })
        if not anchors: return []

        for t in all_texts:
            closest = min(anchors, key=lambda a: abs(t["cy"] - a['anchor_cy']))
            if abs(t["cy"] - closest['anchor_cy']) < 50:
                closest['items'].append(t)
                
        results = []
        for row in anchors:
            item_data = {"name": row["name"], "is_max_price": False, "target_cartridge": ""}
            equator = row["equator_y"]
            price_texts = [t for t in row["items"] if COL_PRICE_MIN <= t["cx"] < COL_PRICE_MAX]
            cart_texts = [t for t in row["items"] if t["cx"] >= COL_CART_MIN]
            
            top_pct, bot_pct = [], []
            for t in price_texts:
                if t["box"][0] + t["box"][2] > 900:
                    nums = re.findall(r'\d+', t["text"])
                    if nums:
                        if t["cy"] < equator: top_pct.append(nums[-1])
                        else: bot_pct.append(nums[-1])
            
            if top_pct and bot_pct and set(top_pct).intersection(set(bot_pct)):
                item_data["is_max_price"] = True
            
            bot_cart_texts = [t for t in cart_texts if t["cy"] >= equator]
            target_texts = bot_cart_texts if bot_cart_texts else [t for t in cart_texts if t["cy"] < equator]
                
            if target_texts:
                target_texts.sort(key=lambda t: t["cx"]) 
                raw_cart = "".join([t["text"] for t in target_texts])
                item_data["target_cartridge"] = re.sub(r'[^\w\u4e00-\u9fa5]', '', raw_cart)
            
            results.append(item_data)
            
        return results