import os
import json
import re
from maa.custom_action import CustomAction
from maa.context import Context
from maa.agent.agent_server import AgentServer
from utils import mfaalog

# ==========================================
# 列带配置:节点 roi 承载(#380)。py 首跑查询一次并缓存,
# 由 base/pc 分层天然获得平台差异化;节点缺失时回退旧常数。
# ==========================================
_BAND_NODES = {
    "name": "Arbitrage_Sell_Col_Name",
    "price": "Arbitrage_Sell_Col_Price",
    "cart": "Arbitrage_Sell_Col_Cart",
}
_LEGACY_BANDS = {"name": (470, 730), "price": (880, 960), "cart": (960, 1280)}
_BANDS_CACHE = None

# ==========================================
# 子行锚(2026-07-22)：价目表每个商品占两行
#   上行「当前」   = 今天的实际行情(溢价率/该去哪个卡带卖)
#   下行「每月N日」= 该商品每月最高价日的行情(仅供比对,不可当目标)
# 原实现用「商品名底边+7」作赤道分上下,但商品行距仅约73px而分配阈值50px,
# 上一个商品的「每月」行会被吸进本行上半区 → 假交集误判满价(07-22实录:
# 流浪美食家烤肉吃进上行120%与自身120%凑交集);且目标卡带取的是下行,
# 拿到的是"每月最高价日的卡带"而非今天该去的卡带(07-22实录:桑格利亚酒
# 当前118%在剧情游戏卡4,却被判去剧情游戏卡11——那是每月5日才有的120%)。
# 改用行标记文本直接锚定两个子行,彻底隔离跨行污染。
# ==========================================
RE_MARK_CUR = re.compile(r'^[当當]前$')
RE_MARK_MONTH = re.compile(r'^每月')
# 溢价率必须是两三位数+%,排除OCR把装饰符号读成"4"/"A"这类噪声
# (07-22实录:(859,358)与(856,391)两处噪声"4"上下各一,凑出假交集)
RE_PCT = re.compile(r'(\d{2,3})\s*%')
SUBROW_TOL = 14      # 同子行 y 容差(子行间距约35px,行距约73px)
CUR_NEAR_NAME = 25   # 「当前」标记与商品名的最大 y 偏差
MONTH_BELOW_CUR = (12, 70)  # 「每月」标记相对「当前」的 y 偏移窗口


def _cart_expected(raw: str) -> str:
    """卡带名 → 容错正则。OCR 常把'剧'读成'则'(07-22实录'则情游戏卡3'),
    故前缀模糊、卡号精确;(?!\\d) 防止卡4误匹配卡41。"""
    m = re.search(r'(\d+)\s*$', raw)
    if m:
        return r'游[戏戲]卡\s*' + m.group(1) + r'(?!\d)'
    return raw

def _get_bands(context: Context, screenshot) -> dict:
    """经 run_recognition 读取 DirectHit 数据节点的生效 roi(box=roi,引擎合并视图,pc覆盖可见)。
    注:get_node_data 跨 agent 边界对资源节点返回空(07-17 实测),不可用。"""
    global _BANDS_CACHE
    if _BANDS_CACHE is not None:
        return _BANDS_CACHE
    bands = {}
    try:
        for key, node in _BAND_NODES.items():
            reco = context.run_recognition(node, screenshot)
            box = getattr(reco, "box", None) if reco else None
            if box is None:
                raise ValueError(f"{node} 识别未返回box")
            try:
                x, w = box.x, box.w
            except AttributeError:
                x, _, w, _ = box
            if w <= 0:
                raise ValueError(f"{node} roi宽度非法")
            bands[key] = (x, x + w)
        mfaalog.info(f"[Arbitrage] 📐 列带已从节点解析: 名{bands['name']} 价{bands['price']} 卡带{bands['cart']}")
    except Exception as e:
        bands = dict(_LEGACY_BANDS)
        mfaalog.warning(f"[Arbitrage] ⚠️ 列带节点查询失败({e}),回退内置常数(安卓布局)")
    _BANDS_CACHE = bands
    return bands

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
            # 卡带名走容错正则(OCR 把'剧'读成'则'等,前缀模糊卡号精确)
            cart_pat = _cart_expected(cart_raw)
            if cart_pat != cart_raw:
                mfaalog.info(f"[Arbitrage]   ↳ 卡带匹配用容错式: {cart_pat}")
            override_cfg = {
                "Arbitrage_Sell_PackShopSwich": {
                    "expected": cart_pat
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
        # 配置区(EQUATOR为y向行内分界,两端一致)
        EQUATOR_OFFSET = 7

        screenshot = context.tasker.controller.post_screencap().wait().get()
        # 新增的防御逻辑：如果截图失败，记录警告并安全退出当前解析
        if screenshot is None:
            print("[Arbitrage] ❌ 严重错误: 底层截图获取失败 (返回 None)！跳过当前页解析。")
            return []

        # 列带来自数据节点roi(#380),首跑经run_recognition解析并缓存
        bands = _get_bands(context, screenshot)
        COL_NAME_MIN, COL_NAME_MAX = bands["name"]
        COL_PRICE_MIN, COL_PRICE_MAX = bands["price"]
        COL_CART_MIN, COL_CART_MAX = bands["cart"]
        # 价格%列右缘枢轴:随价带派生(安卓 960-60=900 与原硬编码严格一致)
        PRICE_EDGE = COL_PRICE_MAX - 60
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
                
        # 行标记文本(在价格列左侧的独立列,不属于任何数据列带)
        cur_marks = [t for t in all_texts
                     if RE_MARK_CUR.match(re.sub(r'\s', '', t["text"]))]
        month_marks = [t for t in all_texts if RE_MARK_MONTH.match(t["text"])]

        results = []
        for row in anchors:
            item_data = {"name": row["name"], "is_max_price": False, "target_cartridge": ""}
            ay = row["anchor_cy"]

            # 上子行锚:与商品名同高的「当前」
            cur_cands = [m for m in cur_marks if abs(m["cy"] - ay) <= CUR_NEAR_NAME]
            if not cur_cands:
                # 找不到行标记宁可不卖(错卖代价 >> 漏卖代价)
                mfaalog.warning(
                    f"[Arbitrage] ⚠️ [{row['name']}] 未找到「当前」行标记,本行不参与满价判定"
                )
                results.append(item_data)
                continue
            cur_y = min(cur_cands, key=lambda m: abs(m["cy"] - ay))["cy"]

            # 下子行锚:「当前」下方窗口内最近的「每月N日」
            _lo, _hi = MONTH_BELOW_CUR
            mon_cands = [m for m in month_marks if _lo < (m["cy"] - cur_y) < _hi]
            mon_y = (min(mon_cands, key=lambda m: m["cy"] - cur_y)["cy"]
                     if mon_cands else None)

            def _pcts(y, _texts=all_texts, _lo=COL_PRICE_MIN, _hi2=COL_PRICE_MAX):
                if y is None:
                    return []
                out = []
                for t in _texts:
                    if not (_lo <= t["cx"] < _hi2):
                        continue
                    if abs(t["cy"] - y) > SUBROW_TOL:
                        continue
                    m = RE_PCT.search(t["text"])
                    if m:
                        out.append(m.group(1))
                return out

            top_pct = _pcts(cur_y)
            bot_pct = _pcts(mon_y)

            # 已达满价 = 今天的溢价率 与 每月最高价档 相同
            if top_pct and bot_pct and set(top_pct).intersection(set(bot_pct)):
                item_data["is_max_price"] = True

            # 目标卡带取「当前」行:今天该去哪卖。下行是每月最高价日的卡带,取了会白跑
            cart_texts = [t for t in all_texts
                          if COL_CART_MIN <= t["cx"] < COL_CART_MAX
                          and abs(t["cy"] - cur_y) <= SUBROW_TOL]
            if cart_texts:
                cart_texts.sort(key=lambda t: t["cx"])
                raw_cart = "".join([t["text"] for t in cart_texts])
                item_data["target_cartridge"] = re.sub(r'[^\w一-龥]', '', raw_cart)

            results.append(item_data)

        return results