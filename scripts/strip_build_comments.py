#!/usr/bin/env python3
"""剥离构建产物里的注释：仓库保留注释，发出去的包不带。

默认作用于 install/，即 install.py 组装完的产物目录。范围靠**两级筛**框定
（见 SCAN_JSON / SCAN_PY / JSON_TYPE_EXCLUDE）：先目录白名单，再文件类型。

1. <root>/resource/*/pipeline/**/*.json 与 <root>/resource/*/default_pipeline.json
   —— 删除 desc / doc 两个注释字段，并去掉 JSONC 风格的 // 与 /* */ 注释。
2. <root>/agent/**/*.py —— 删除 # 注释与 docstring（模块/类/函数的文档字符串）。

白名单之外的 json 一概碰不到：interface.json 在产物根、mfa_layout.json 在
resource/ 根，都不在任何白名单目录内，将来 resource/ 下新增配置文件也天然安全。

⚠️ Python 里没有 // 注释——那是整除运算符（len(xs) // 2）。本脚本只按
tokenize 给出的 COMMENT token 下刀，绝不对 .py 做 // 文本匹配，否则
arbitrage_result.py 里的整除会被截断成语法错误。仓库里那些 // 出现在
cartridge_lib.py / ocr_decision.py 的 # 注释内部（是贴在注释里的 JSON 用法
示例），会随所在的 # 注释一并消失。

⚠️ JSON 的 // 同样不能用正则删：EventBattle.json 有一条 OCR 正则
"expected": "[//?]"，正则删注释会把它腰斩。所以走字符串状态机词法扫描。
当前仓库里 resource 下并无 JSONC 注释，这部分是为将来预留的。

⚠️ docstring 是**语法结构**不是词法记号，tokenize 认不出它——三引号字符串在
token 流里跟普通字符串一模一样。所以这部分走 ast：只认 Module / ClassDef /
FunctionDef / AsyncFunctionDef 体内第一条语句是纯字符串常量的情形。
函数体里只剩这一条时补 pass（否则语法错误），复用它原来的起始行以免行号错位。

⚠️ docstring 和 # 注释有个本质区别：# 注释在词法阶段就没了，docstring 会进
字节码、挂在 __doc__ 上，运行时读得到。当前 agent/ 里没人读它（死数据），
但 find_doc_consumers() 每次都要复查一遍——一旦有人开始读，剥离就成了静默的
行为变更，必须让构建停下来。

默认保留被删整行注释留下的空行，让产物与仓库源码**行号一一对应**——
线上用户回传的 traceback 才能直接定位到仓库里的那一行。加 --squeeze 可关掉。

用法:
    python scripts/strip_build_comments.py [root] [--dry-run] [--squeeze]
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import tokenize
from pathlib import Path

# 注释字段：值必须是字符串才删。pipeline 顶层 key 是节点名，
# 万一有节点叫 desc，删掉就是整段流程消失——所以顶层一律不碰。
COMMENT_KEYS = ("desc", "doc")

# ---- 第一级：目录白名单（相对产物根）----------------------------------------
# 与其扫遍 resource/ 再靠黑名单往外摘，不如只认 pipeline 协议文件所在的位置。
# 白名单之外的 json 全都碰不到，将来 resource/ 下新增任何配置文件都天然安全。
SCAN_JSON = (
    "resource/*/pipeline/**/*.json",  # 各资源包（base / pc / …）的节点文件；** 兼容将来分子目录
    "resource/*/default_pipeline.json",  # 节点默认值，不在 pipeline/ 内但同属 pipeline 协议
)
SCAN_PY = ("agent/**/*.py",)

# ---- 第二级：白名单目录内按文件类型剔除 --------------------------------------
# *.mpe.json 是 MaaPipelineEditor 的编辑器工程文件（画布坐标等），结构与节点协议
# 完全不同，且**就躺在 pipeline/ 目录里**，第一级筛不掉。.gitignore 第 470 行忽略
# 了它们，CI 的干净 checkout 里不存在；但本地跑 install.py 时 copytree 会把它们
# 一并拷进 install/，所以必须在这一级显式挡掉。
JSON_TYPE_EXCLUDE = ("*.mpe.json",)

# 这两个文件不在任何白名单目录内（interface.json 在产物根，mfa_layout.json 在
# resource/ 根）。它们要是出现在扫描结果里，只可能是白名单被改坏了——那属于
# 需要有人来看一眼的事故，报错退出，不要默默跳过。
NEVER_SCAN = {"interface.json", "mfa_layout.json"}

# 收尾审计放行的文件：schema 归 MFAAvalonia 管，将来它正经加个 desc 字段也不算残留。
AUDIT_JSON_IGNORE = {"mfa_layout.json"}

# ---- docstring 消费者哨兵 ----------------------------------------------------
# docstring 不是纯注释：# 注释在词法阶段就没了，docstring 会进字节码、挂在
# __doc__ 上，运行时读得到。删它唯一可能改变行为的路径，就是有人真去读 __doc__。
#
# 当前 agent/ 里一处都没有（含 MaaFramework 的 maa 包本身，17 个 py 源码零引用，
# custom_action / custom_recognition 都是显式传名字注册的），所以它是死数据。
# 但"现在没人读"不等于"将来没人写"——哪天有人加个 doctest、或写
# ArgumentParser(description=__doc__)，剥离就成了静默的行为变更：CI 全绿，
# 包发出去帮助文本是空的。与 NEVER_SCAN 同一种设计：宁可让构建停下来。
DOC_CONSUMER_ATTRS = {"getdoc"}  # inspect.getdoc(obj)
DOC_CONSUMER_MODULES = {"doctest", "pydoc"}


class StripError(Exception):
    pass


def collect(root: Path, patterns: tuple[str, ...], type_exclude: tuple[str, ...] = ()) -> tuple[list[Path], int]:
    """两级筛：先按目录白名单收集，再按文件类型剔除。返回 (命中列表, 剔除数)。"""
    hits: set[Path] = set()
    for pat in patterns:
        hits.update(p for p in root.glob(pat) if p.is_file())
    kept = [p for p in sorted(hits) if not any(p.match(x) for x in type_exclude)]
    return kept, len(hits) - len(kept)


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


def strip_jsonc_comments(text: str) -> str:
    """按字符串状态机去掉 // 与 /* */ 注释，字符串字面量内的斜杠原样保留。"""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                out.append(text[i : i + 2])
                i += 2
                continue
            if c == '"':
                in_str = False
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                j = text.find("\n", i)
                i = n if j == -1 else j  # 换行留着，报错行号才准
                continue
            if text[i + 1] == "*":
                j = text.find("*/", i + 2)
                end = n if j == -1 else j + 2
                out.append("\n" * text.count("\n", i, end))
                i = end
                continue
        out.append(c)
        i += 1
    return "".join(out)


def strip_trailing_commas(text: str) -> str:
    """去掉 } / ] 前的尾逗号（JSONC 允许，标准 json 不允许）。"""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                out.append(text[i : i + 2])
                i += 2
                continue
            if c == '"':
                in_str = False
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                out.append(text[i + 1 : j])  # 逗号丢掉，中间空白留着
                i = j
                continue
        out.append(c)
        i += 1
    return "".join(out)


def prune_comment_keys(node, stats: dict, depth: int, where: str) -> None:
    """递归删除注释字段。只删深度 >0、值为字符串的 desc / doc。"""
    if isinstance(node, dict):
        for key in list(node):
            value = node[key]
            if key in COMMENT_KEYS:
                if depth == 0:
                    print(f"::warning::{where}: 顶层出现 {key!r}，视作节点名保留")
                elif not isinstance(value, str):
                    print(f"::warning::{where}: {key!r} 的值不是字符串（{type(value).__name__}），保留")
                else:
                    del node[key]
                    stats[key] += 1
                    continue
            prune_comment_keys(value, stats, depth + 1, where)
    elif isinstance(node, list):
        for value in node:
            prune_comment_keys(value, stats, depth + 1, where)


def process_json(path: Path, stats: dict, dry_run: bool) -> None:
    raw = path.read_text(encoding="utf-8-sig")
    text = strip_trailing_commas(strip_jsonc_comments(raw))
    had_comments = text != raw

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise StripError(f"{path}: 去注释后无法解析: {e}") from e

    before = dict(stats)
    prune_comment_keys(data, stats, 0, str(path))
    dropped = sum(stats[k] - before[k] for k in COMMENT_KEYS)

    if not dropped and not had_comments:
        stats["json_untouched"] += 1
        return

    new_text = json.dumps(data, ensure_ascii=False, indent=4) + "\n"
    # 回读自校验：写出去的东西必须还能解析成同一个对象
    if json.loads(new_text) != data:
        raise StripError(f"{path}: 序列化回读不一致")

    stats["json_changed"] += 1
    stats["bytes_saved"] += len(raw.encode("utf-8")) - len(new_text.encode("utf-8"))
    if not dry_run:
        path.write_text(new_text, encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------


DOC_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def find_docstrings(src: str, lines: list[str]) -> tuple[list[tuple[ast.AST, ast.Expr]], list[str]]:
    """找出可安全删除的 docstring。返回 ([(宿主, 语句)], 因形状不安全跳过的说明)。

    剥离与收尾审计共用这一份判据，两边口径必须一致——否则「跳过没删」会被审计
    当成残留报错，一个合法写法就能把构建卡死。

    只认「体内第一条语句是纯字符串常量」的 ast 结构——类型注解里的字符串
    （-> "str | None"）、赋值给变量的三引号文本都不在此列，碰都不会碰。

    形状闸：docstring 必须独占它所在的整行区间（前后同行没有别的代码），
    否则跳过。把定义和文档挤在同一行的那种写法删了就是空函数体，
    与其猜怎么补，不如原样保留、让人看见。
    """
    hits: list[tuple[ast.AST, ast.Expr]] = []
    skipped: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, DOC_OWNERS) or not node.body:
            continue
        first = node.body[0]
        if not isinstance(first, ast.Expr):
            continue
        if not (isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)):
            continue
        head = lines[first.lineno - 1][: first.col_offset]
        tail = lines[(first.end_lineno or first.lineno) - 1][first.end_col_offset :]
        if head.strip() or tail.strip():
            skipped.append(f"L{first.lineno}: docstring 与代码同行，保留")
            continue
        hits.append((node, first))
    return hits, skipped


def find_doc_consumers(src: str) -> list[str]:
    """找出会读 docstring 的用法。返回人话描述，空列表 = 这个文件删 docstring 是安全的。

    走 ast 而不是文本 grep：字符串里、注释里出现 "__doc__" 三个字不算数，
    而剥离恰恰会把注释删掉——用 grep 会在删注释前后给出不同答案。
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Attribute):
            if node.attr == "__doc__":
                found.append(f"L{node.lineno}: 读取 .__doc__")
            elif node.attr in DOC_CONSUMER_ATTRS:
                found.append(f"L{node.lineno}: 调用 {node.attr}()")
        elif isinstance(node, ast.Name) and node.id == "__doc__":
            found.append(f"L{node.lineno}: 引用模块级 __doc__")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in DOC_CONSUMER_MODULES:
                    found.append(f"L{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in DOC_CONSUMER_MODULES:
                found.append(f"L{node.lineno}: from {node.module} import ...")
    return found


def strip_py_docstrings(src: str, squeeze: bool) -> tuple[str, int, list[str]]:
    """删掉 docstring。返回 (新源码, 删除条数, 跳过说明)。"""
    lines = src.splitlines(keepends=True)
    hits, skipped = find_docstrings(src, lines)
    if not hits:
        return src, 0, skipped

    kill: dict[int, str | None] = {}  # 行号(1基) -> None=删成空行 / 字符串=替换成它
    for node, first in hits:
        for row in range(first.lineno, (first.end_lineno or first.lineno) + 1):
            kill[row] = None
        # 体里只有这一条 → 删完就是空体。模块可以为空，函数/类不行，补 pass。
        if len(node.body) == 1 and not isinstance(node, ast.Module):  # type: ignore[attr-defined]
            line = lines[first.lineno - 1]
            eol = line[len(line.rstrip("\r\n")) :] or "\n"
            kill[first.lineno] = f"{line[: first.col_offset]}pass{eol}"

    out: list[str] = []
    for idx, line in enumerate(lines, start=1):
        if idx not in kill:
            out.append(line)
            continue
        replacement = kill[idx]
        if replacement is not None:
            out.append(replacement)
        elif not squeeze:
            out.append(line[len(line.rstrip("\r\n")) :] or "\n")  # 留空行，行号对齐
    return "".join(out), len(hits), skipped


def strip_py_comments(src: str, squeeze: bool) -> tuple[str, int]:
    """删掉 # 注释。返回 (新源码, 删除的注释数)。

    只信 tokenize 的 COMMENT token —— 字符串字面量、f-string 里的 # 都不会被
    误判，整除运算符 // 更是碰都不碰。docstring 由 strip_py_docstrings 单独处理。
    """
    lines = src.splitlines(keepends=True)
    cuts: dict[int, int] = {}  # 行号(1基) -> 注释起始列
    count = 0
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            row, col = tok.start
            # 一行至多一个 COMMENT token；min 只是防御
            cuts[row] = min(cuts.get(row, col), col)
            count += 1

    out: list[str] = []
    for idx, line in enumerate(lines, start=1):
        if idx not in cuts:
            out.append(line)
            continue
        head = line[: cuts[idx]]
        if head.strip():
            # 行尾注释：截断，保留原行尾（\n / \r\n）
            tail = line[len(line.rstrip("\r\n")) :]
            out.append(head.rstrip() + tail)
        elif squeeze:
            continue  # 整行注释，连行一起删
        else:
            out.append(line[len(line.rstrip("\r\n")) :] or "\n")  # 留空行，行号对齐
    return "".join(out), count


def process_py(path: Path, stats: dict, dry_run: bool, squeeze: bool) -> None:
    src = path.read_text(encoding="utf-8-sig")
    # 先 docstring 后 # 注释：docstring 靠 ast 的行号定位，必须在原始源码上算。
    # 反过来先删注释会在 squeeze 模式下把行号挪走，ast 记的位置就全错了。
    try:
        stage1, docs, skipped = strip_py_docstrings(src, squeeze)
    except SyntaxError as e:
        raise StripError(f"{path}: 解析失败（行 {e.lineno}）: {e.msg}") from e
    for msg in skipped:
        print(f"::warning::{path}: {msg}")
    try:
        new_src, count = strip_py_comments(stage1, squeeze)
    except tokenize.TokenError as e:
        raise StripError(f"{path}: 词法分析失败: {e}") from e

    if not count and not docs:
        stats["py_untouched"] += 1
        return

    # 语法自校验：产物必须还能编译，否则 Agent 到用户手上直接起不来
    try:
        compile(new_src, str(path), "exec")
    except SyntaxError as e:
        raise StripError(f"{path}: 去注释后语法错误（行 {e.lineno}）: {e.msg}") from e

    stats["py_changed"] += 1
    stats["comments"] += count
    stats["docstrings"] += docs
    stats["bytes_saved"] += len(src.encode("utf-8")) - len(new_src.encode("utf-8"))
    if not dry_run:
        path.write_text(new_src, encoding="utf-8", newline="")


# --------------------------------------------------------------------------
# 收尾审计
# --------------------------------------------------------------------------


def find_comment_keys(node, depth: int = 0) -> list[str]:
    """递归找出残留的 desc / doc（判据与 prune_comment_keys 一致）。"""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if depth > 0 and key in COMMENT_KEYS and isinstance(value, str):
                found.append(key)
            found.extend(find_comment_keys(value, depth + 1))
    elif isinstance(node, list):
        for value in node:
            found.extend(find_comment_keys(value, depth + 1))
    return found


def audit(root: Path) -> tuple[list[str], int, int]:
    """按结果复查产物，不看白名单。返回 (问题列表, resource 下 json 总数, 已查数)。

    白名单是按目录写死的。目录一旦大改——新加一个资源包、pipeline 改名、节点文件
    多套一层子目录——白名单会静默失配：少处理一批文件，而前面所有哨兵（扫不到文件、
    NEVER_SCAN 入侵）一个都不会响，带注释的产物照样发出去。

    所以收尾时不问"白名单覆盖了谁"，直接问"产物里还有没有注释"。目录怎么变都拦得住。
    """
    problems: list[str] = []
    total = checked = 0

    res_dir = root / "resource"
    if res_dir.is_dir():
        for path in sorted(res_dir.rglob("*.json")):
            total += 1
            if path.name in AUDIT_JSON_IGNORE or any(path.match(x) for x in JSON_TYPE_EXCLUDE):
                continue
            checked += 1
            raw = path.read_text(encoding="utf-8-sig")
            try:
                # 刻意用标准 json 而非 jsonc：剥离后的产物就该是纯 JSON，
                # 解析失败本身即"注释没删干净"的证据。
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                problems.append(f"{path}: 不是纯 JSON（疑似残留注释）: {e}")
                continue
            leftover = find_comment_keys(data)
            if leftover:
                problems.append(f"{path}: 仍残留注释字段 {sorted(set(leftover))} 共 {len(leftover)} 处")

    agent_dir = root / "agent"
    if agent_dir.is_dir():
        for path in sorted(agent_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            src = path.read_text(encoding="utf-8-sig")
            try:
                n = sum(1 for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type == tokenize.COMMENT)
            except tokenize.TokenError as e:
                problems.append(f"{path}: 词法分析失败: {e}")
                continue
            if n:
                problems.append(f"{path}: 仍残留 {n} 处 # 注释")
            # docstring 用与剥离侧完全相同的判据复查：形状闸跳过的那些不算残留
            # （它们剥离时已经打过 warning），否则一个合法的单行写法就能卡死构建。
            try:
                hits, _ = find_docstrings(src, src.splitlines(keepends=True))
            except SyntaxError as e:
                problems.append(f"{path}: 解析失败（行 {e.lineno}）: {e.msg}")
                continue
            if hits:
                problems.append(f"{path}: 仍残留 {len(hits)} 条 docstring")

    return problems, total, checked


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="剥离构建产物中的注释")
    ap.add_argument("root", nargs="?", default="install", help="产物根目录（默认 install）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写盘")
    ap.add_argument("--squeeze", action="store_true", help="整行注释连空行一起删（会打乱行号）")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"::error::产物目录不存在: {root.resolve()}")
        return 1

    stats = dict.fromkeys(
        (
            "desc",
            "doc",
            "comments",
            "docstrings",
            "json_changed",
            "json_untouched",
            "py_changed",
            "py_untouched",
            "bytes_saved",
        ),
        0,
    )
    errors: list[str] = []

    res_dir = root / "resource"
    agent_dir = root / "agent"
    json_files, skipped = collect(root, SCAN_JSON, JSON_TYPE_EXCLUDE)
    py_files, _ = collect(root, SCAN_PY)
    py_files = [p for p in py_files if "__pycache__" not in p.parts]

    # 白名单写坏了才可能把这两个扫进来，属于要人来看一眼的事故
    intruders = [p for p in json_files if p.name in NEVER_SCAN]
    if intruders:
        for p in intruders:
            print(f"::error::{p} 不该进入扫描范围，请检查 SCAN_JSON 白名单")
        return 1

    # 目标目录存在却一个文件都没扫到 = 产物布局变了，不能静默放过
    if res_dir.is_dir() and not json_files:
        print(f"::error::{res_dir} 下未匹配到任何 pipeline json，产物布局可能已变更")
        return 1
    if agent_dir.is_dir() and not py_files:
        print(f"::error::{agent_dir} 下未找到任何 py，产物布局可能已变更")
        return 1
    if not res_dir.is_dir() and not agent_dir.is_dir():
        print(f"::error::{root} 下既无 resource 也无 agent 目录")
        return 1

    # 有人在读 docstring = 删它就是静默的行为变更，停下来让人看一眼
    consumers: list[str] = []
    for path in py_files:
        try:
            consumers.extend(f"{path} {hit}" for hit in find_doc_consumers(path.read_text(encoding="utf-8-sig")))
        except SyntaxError as e:
            print(f"::error::{path}: 解析失败（行 {e.lineno}）: {e.msg}")
            return 1
    if consumers:
        print("::error::有代码在运行时读 docstring，剥离会静默改掉它的行为：")
        for msg in consumers[:20]:
            print(f"::error::  {msg}")
        if len(consumers) > 20:
            print(f"::error::（另有 {len(consumers) - 20} 处未列出）")
        print("::error::改掉这些用法，或给 strip_py_docstrings 加开关把剥离改成可选")
        return 1

    for path in json_files:
        try:
            process_json(path, stats, args.dry_run)
        except StripError as e:
            errors.append(str(e))
    for path in py_files:
        try:
            process_py(path, stats, args.dry_run, args.squeeze)
        except StripError as e:
            errors.append(str(e))

    mode = "[dry-run] " if args.dry_run else ""
    if skipped:
        print(f"{mode}json: 跳过 {skipped} 个编辑器工程文件（*.mpe.json）")
    print(f"{mode}json: 改写 {stats['json_changed']} / 未变 {stats['json_untouched']} 个")
    print(f"{mode}      删除注释字段 desc {stats['desc']} 处、doc {stats['doc']} 处")
    print(
        f"{mode}py  : 改写 {stats['py_changed']} / 未变 {stats['py_untouched']} 个，"
        f"删除 # 注释 {stats['comments']} 处、docstring {stats['docstrings']} 条"
    )
    print(f"{mode}合计瘦身 {stats['bytes_saved'] / 1024:.1f} KB")

    if errors:
        for msg in errors:
            print(f"::error::{msg}")
        return 1

    # 收尾审计：dry-run 时产物没动过，审计必然满地残留，跳过
    if not args.dry_run:
        problems, total, checked = audit(root)
        print(f"审计: resource 下 json 共 {total} 个，复查 {checked} 个；agent 下 py 全查")
        if problems:
            print(f"::error::审计发现 {len(problems)} 处残留——白名单很可能已与产物目录结构失配")
            for msg in problems[:20]:
                print(f"::error::{msg}")
            if len(problems) > 20:
                print(f"::error::（另有 {len(problems) - 20} 处未列出）")
            return 1
        print("审计: ✅ 产物中无残留注释")

    # 一处都没删 = 要么扫错了目录/产物布局变了，要么这份产物已经剥过一遍。
    # 前者必须让构建停下来（静默通过正是 exec-bit 那次翻车的方式），
    # 后者在 CI 里不会发生——每个 job 只跑一次，所以这里一律按失败处理。
    if not (stats["desc"] or stats["doc"] or stats["comments"] or stats["docstrings"]):
        print("::error::没有删掉任何注释：目标目录或产物布局可能有误（若是对同一产物重复执行，属预期）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
