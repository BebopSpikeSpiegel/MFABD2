import json
import re
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context
import utils

# ==============================================================================
# 📜 OCR 决策排序器 (OCR_RankAndPatch v3.0) - 全参数说明书
# ==============================================================================
# [功能简介]
# 1. 自动获取当前节点的 OCR 结果。
# 2. 根据物理位置 (横向/纵向) 重新排序，确保与 replacement_list 一一对应。
# 3. 清洗数据 (支持整数/小数模式)，根据数值大小进行排序 (升序/降序)。
# 4. 选中排名第 N 的数据，将其对应的 "替换值" 注入到 下一个节点的指定字段。
#
# [参数详解 (custom_action_param)]
#
# 1. 基础逻辑参数:
#    - "replacement_list": [List] (必填) 替换值列表。按屏幕物理顺序 (上->下 或 左->右) 填写。
#                                  例如: [[200,100,0,0], [200,200,0,0]]
#    - "target_node":      [String] (必填) 要修改参数的目标节点名称。
#    - "target_param":     [String] (选填) 要注入的字段名。
#                                  * 目标是 Click 动作 -> 填 "target" (默认)
#                                  * 目标是 Swipe 动作 -> 填 "begin"
#                                  * 目标是 Custom 动作 -> 填 "custom_action_param"
#
# 2. 排序与筛选参数:
#    - "direction":   [String] (选填) 物理排序方向。决定 replacement_list 对应屏幕的顺序。
#                     * "vertical"   (默认): 从上到下排 (Y轴)。适合垂直列表。
#                     * "horizontal"       : 从左到右排 (X轴)。适合横排卡片。
#    - "sort_mode":   [String] (选填) 数值排序方式。
#                     * "asc"  (默认): 升序 (从小到大)，选最小值/最便宜。
#                     * "desc"       : 降序 (从大到小)，选最大值/战斗力最高。
#    - "pick_index":  [Int]    (选填) 选第几名。
#                     * 1 (默认): 选第 1 名 (最大/最小)。
#                     * 2       : 选第 2 名，以此类推。
#
# 3. 数据清洗参数:
#    - "number_mode": [String] (选填) 数字解析模式。
#                     * "float" (默认): 保留小数点。例如 "19.63" 识别为 19.63。
#                     * "int"         : 强制整数。去除逗号和小数点。
#                                       例如 "19,630" 或 "19.630" 都会被视为 19630。
#                                       (解决 OCR 误把千分位识别为小数点的问题)
#    - "filter_regex": [String] (选填) 提取数字的正则。默认为 "(\\d+)"。
# ==============================================================================
#     // =======================================================
#     // 范例场景：横向排列了3个宝箱，需要点击金币最多的那个
#     // =======================================================
# {
#     "Select_Best_Chest": {
#         // 1. 开启 OCR 能力 (必须!)
#         "recognition": "OCR",
#         "expected": "(\\d+)", // 只要有数字就放行
#         "roi": [ 100, 200, 500, 100 ], // 识别区域

#         // 2. 调用 Python 决策工具
#         "action": "Custom",
#         "custom_action": "OCR_RankAndPatch",
        
#         // 3. 全参数配置字典
#         "custom_action_param": {
#             // --- A. 注入配置 (必填) ---
#             // 你的"弹药库"，对应屏幕上物理位置的坐标
#             "replacement_list": [
#                 [ 200, 300, 0, 0 ], // 对应屏幕上 第1个 (最左边/最上边)
#                 [ 400, 300, 0, 0 ], // 对应屏幕上 第2个
#                 [ 600, 300, 0, 0 ]  // 对应屏幕上 第3个
#             ],
#             // 要修改哪个节点？
#             "target_node": "Click_Chest",
#             // 要修改哪个字段？(Click用 "target", Swipe用 "begin")
#             "target_param": "target",

#             // --- B. 排序方向 (选填) ---
#             // "vertical"   = 从上到下 (默认，适合列表)
#             // "horizontal" = 从左到右 (适合横排)
#             "direction": "horizontal",

#             // --- C. 筛选逻辑 (选填) ---
#             // "asc"  = 选最小/最少/最便宜 (默认)
#             // "desc" = 选最大/最多/战力最高
#             "sort_mode": "desc",
            
#             // --- D. 选第几名 (选填) ---
#             // 1 = 冠军, 2 = 亚军 ... (默认 1)
#             "pick_index": 1,

#             // --- E. 数字清洗 (选填) ---
#             // "float" = 保留小数 (默认)
#             // "int"   = 强制整数 (去除逗号和小数点，防止OCR误判)
#             "number_mode": "int",
            
#             // 正则表达式 (默认提取所有数字)
#             "filter_regex": "(\\d+)"
#         },
        
#         "next": [ "Click_Chest" ]
#     },

#     // 目标节点 (被注入的对象)
#     "Click_Chest": {
#         "action": "Click",
#         "target": [ 0, 0, 0, 0 ], // 这里会被自动替换成金币最多的那个坐标
#         "next": [ "Next_Task" ]
#     }
# }
# ==============================================================================

@AgentServer.custom_action("OCR_RankAndPatch")
class OCR_RankAndPatch(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            utils.mfaalog.info("[Py] 🚀 OCR_Rank v3.0 启动...")

            # --- 1. 参数解析 ---
            if not argv.custom_action_param: return False
            params = json.loads(str(argv.custom_action_param).strip())

            # 核心参数
            replacement_list = params.get("replacement_list", [])
            target_node = params.get("target_node")
            # 默认注入 Click 的 target 字段，比较常用
            target_param = params.get("target_param", "target")
            
            # 逻辑参数
            number_mode = params.get("number_mode", "float")  # 先获取 number_mode
            sort_mode = params.get("sort_mode", "asc")
            direction = params.get("direction", "vertical")
            
            # 智能分配默认正则
            if number_mode == "float" and "filter_regex" not in params:  
                filter_regex = r"(\d+\.?\d*)"  # float 模式默认保留小数
            else:  
                filter_regex = params.get("filter_regex", r"(\d+)")
            
            # 索引转换 (人类 1-based -> 内部 0-based)
            user_pick = int(params.get("pick_index", 1))
            internal_pick_index = user_pick - 1

            if not (replacement_list and target_node):
                utils.mfaalog.error("[Py] ❌ 缺少必要参数 (replacement_list / target_node)")
                return False

            if internal_pick_index < 0:
                utils.mfaalog.error(f"[Py] ❌ pick_index 必须 >= 1")
                return False

            # --- 2. 获取 OCR 列表 ---
            raw_reco = getattr(argv, "reco_detail", None)
            ocr_items = []
            
            # 兼容性获取
            all_res = getattr(raw_reco, "all_results", None)
            filtered_res = getattr(raw_reco, "filtered_results", None)
            if all_res: ocr_items = all_res
            elif filtered_res: ocr_items = filtered_res
            elif isinstance(raw_reco, dict):
                if "detail" in raw_reco and "all" in raw_reco["detail"]: ocr_items = raw_reco["detail"]["all"]
                elif "all" in raw_reco: ocr_items = raw_reco["all"]
            elif isinstance(raw_reco, list): ocr_items = raw_reco

            if not ocr_items:
                utils.mfaalog.warning(f"[Py] ⚠️ 未提取到 OCR 列表")
                return False

            # --- 3. 物理排序 (Physical Sort) ---
            def get_sort_key(item):
                box = getattr(item, "box", None)
                if box is None and isinstance(item, dict): box = item.get("box")
                
                x, y = 99999, 99999
                if box:
                    if hasattr(box, "x") and hasattr(box, "y"):
                        x, y = box.x, box.y
                    elif hasattr(box, "__getitem__") and len(box) > 1:
                        x, y = box[0], box[1]
                
                # 根据方向决定排序键
                return x if direction == "horizontal" else y
            
            ocr_items.sort(key=get_sort_key)

            # --- 4. 数据清洗 ---
            clean_data = []
            for idx, item in enumerate(ocr_items):
                text = getattr(item, "text", None)
                if text is None and isinstance(item, dict): text = item.get("text", "")
                if text is None: text = str(item)

                matches = []
                if number_mode == "int":
                    # 暴力清洗：将所有 非数字 (0-9) 的字符全部替换为空
                    # 作用：把 "29:717" 变成 "29717"，把 "1,234" 变成 "1234"
                    clean_text = re.sub(r"[^\d]", "", text)
                    matches = [clean_text] if clean_text else []
                else:
                    # float 模式保持原样，仅去除常见干扰
                    clean_text = text.replace(",", "")
                    # 使用用户传入的 filter_regex，而不是写死的正则
                    try:
                        matches = re.findall(filter_regex, clean_text)
                    except Exception as e:
                        utils.mfaalog.error(f"[Py] 正则匹配错误: {e}，回退默认正则")
                        matches = re.findall(r"(\d+\.?\d*)", clean_text)
                
                if matches:
                    # 如果用户的正则有多个括号(捕获组)，findall 会返回 tuple 列表
                    first_match = matches[0]
                    if isinstance(first_match, tuple):
                        # 获取元组中第一个非空的有效分组
                        first_match = next((g for g in first_match if g), None)
                        if first_match is None:
                            continue  # 如果全是空的，跳过该条目
                        
                    try:
                        val = float(first_match)
                        clean_data.append({
                            "val": val,
                            "original_idx": idx, # 物理位置索引
                            "text": text
                        })
                    except ValueError:
                        pass # 转换浮点数失败则跳过该条目
            
            if not clean_data:
                utils.mfaalog.warning(f"[Py] ⚠️ 无有效数字")
                return False

            # --- 5. 数值排序 ---
            reverse = True if sort_mode == "desc" else False
            sorted_data = sorted(clean_data, key=lambda x: x["val"], reverse=reverse)
            
            # Log
            log_str = " | ".join([
                f"{int(x['val']) if number_mode=='int' else x['val']}(第{x['original_idx']+1}个)" 
                for x in sorted_data
            ])
            utils.mfaalog.info(f"[Py] 数值排序: {log_str}")

            # Pick
            if internal_pick_index >= len(sorted_data):
                utils.mfaalog.error(f"[Py] ❌ 排名 {user_pick} 超出范围 (数据量: {len(sorted_data)})")
                return False
                
            winner = sorted_data[internal_pick_index]
            target_idx = winner['original_idx']

            utils.mfaalog.info(f"[Py] 🏆 选中: {winner['text']} (物理位置: 第 {target_idx+1} 个)")

            # --- 6. 注入 ---
            if target_idx >= len(replacement_list):
                utils.mfaalog.error(f"[Py] ❌ 替换表不足 (需要第 {target_idx+1} 个)")
                return False

            injection_value = replacement_list[target_idx]
            
            context.override_pipeline({
                target_node: {
                    target_param: injection_value
                }
            })
            utils.mfaalog.info(f"[Py] 💉 注入: {injection_value} -> {target_node}")
            
            return True

        except Exception as e:
            utils.mfaalog.error(f"[Py] 异常: {e}")
            return False