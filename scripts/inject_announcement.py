#!/usr/bin/env python3
"""
公告注入工具 (CI专用 - 基于 install.py)
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from version_rules import (
    is_valid_formal_version, is_valid_beta_version,
    is_valid_alpha_version, is_valid_ci_version,
)


MAX_CONTENT_LENGTH = 5000


def _get_tag_type(tag_name: str):
    """将 tag 名映射为内容目标类型（stable/beta/alpha/ci），无法识别返回 None。
    ⚠️ 若版本类型标识发生变更，需与 release/app_msg.md 头部注释同步修改。
    """
    if is_valid_formal_version(tag_name):
        return 'stable'
    if is_valid_beta_version(tag_name):
        return 'beta'
    if is_valid_alpha_version(tag_name):
        return 'alpha'
    if is_valid_ci_version(tag_name):
        return 'ci'
    return None


def _parse_targeted_blocks(file_content: str, tag_type: str) -> str:
    """按 ---target: ...--- / ---end--- 标记解析内容块，按顺序拼接所有匹配当前版本类型的块。"""
    # 先剥离 HTML 注释，防止说明文档中的示例块被误匹配
    content = re.sub(r'<!--.*?-->', '', file_content, flags=re.DOTALL)
    block_re = re.compile(r'---target:\s*(.+?)---[ \t]*\n(.*?)---end---', re.DOTALL | re.IGNORECASE)
    matched = []
    for m in block_re.finditer(content):
        targets = [t.strip().lower() for t in m.group(1).split(',')]
        block = m.group(2).strip()
        if block and ('all' in targets or tag_type in targets):
            matched.append(block)
    return '\n\n'.join(matched)


def inject_announcement(tag_name):
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent

    draft_file = repo_root / "release" / "app_msg.md"
    target_file = repo_root / "install" / "resource" / "Announcement" / "1.公告.md"

    print(f"📍 [定位] 脚本位置: {script_dir}")
    print(f"📄 [调试] 草稿路径: {draft_file}")
    print(f"📍 [定位] 目标路径: {target_file}")

    # --- 1. 检查草稿文件 ---
    if not draft_file.exists():
        print("ℹ️ [跳过] 草稿文件不存在，无需注入")
        return

    # --- 2. 判断版本类型 ---
    tag_type = _get_tag_type(tag_name)
    if tag_type is None:
        print(f"⚠️ [跳过] 无法识别 tag 类型: {tag_name}，跳过注入")
        return

    # --- 3. 解析匹配当前版本类型的内容块 ---
    file_content = draft_file.read_text(encoding='utf-8')
    content = _parse_targeted_blocks(file_content, tag_type)

    if not content:
        print(f"ℹ️ [跳过] release/app_msg.md 中无匹配版本类型 ({tag_type}) 的内容块")
        return

    # --- 4. 检查目标文件 ---
    if not target_file.exists():
        print(f"❌ [错误] 目标文件未找到: {target_file}")
        print("请检查 install.py 是否成功执行了资源复制步骤。")
        install_dir = repo_root / "install"
        if install_dir.exists():
            print(f"📂 install 根目录内容: {[p.name for p in install_dir.iterdir()]}")
            res_dir = install_dir / "resource"
            if res_dir.exists():
                print(f"📂 resource 目录内容: {[p.name for p in res_dir.iterdir()]}")
        sys.exit(1)

    # --- 5. 打印预览 ---
    print("\n" + "=" * 30)
    print(f"📢 准备注入版本: {tag_name}（类型: {tag_type}）")
    print(f"📄 匹配内容预览:\n{content}")
    print("=" * 30 + "\n")

    # --- 6. 执行注入 ---
    original_text = target_file.read_text(encoding='utf-8')
    ANCHOR = "<!-- Msg-Anch -->"

    if ANCHOR not in original_text:
        print(f"❌ [错误] 锚点 '{ANCHOR}' 在目标文件中未找到，注入失败。")
        print("请检查 assets/resource/Announcement/1.公告.md 是否包含锚点标记。")
        sys.exit(1)

    # 消毒：防止草稿内容破坏父级结构或下次匹配点位移
    content = content.replace(ANCHOR, ANCHOR.replace('-->', '- ->'))
    if len(content) > MAX_CONTENT_LENGTH:
        print(f"⚠️ 草稿过长（{len(content)} 字符），截断到 {MAX_CONTENT_LENGTH}")
        content = content[:MAX_CONTENT_LENGTH] + "\n\n*(注：草稿过长已自动截断)*"

    insert_block = f"{ANCHOR}\n\n{content}\n\n---\n"
    new_text = original_text.replace(ANCHOR, insert_block)

    target_file.write_text(new_text, encoding='utf-8')
    print(f"✅ [成功] 公告已注入到: {target_file}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python inject_announcement.py <tag_name>")
        sys.exit(1)

    inject_announcement(sys.argv[1])
