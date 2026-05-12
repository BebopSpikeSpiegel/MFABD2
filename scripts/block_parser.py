import re


def parse_targeted_blocks(file_content: str, tag_type: str) -> str:
    """按 ---target: ...--- / ---end--- 标记解析内容块，按顺序拼接所有匹配当前版本类型的块。

    HTML 注释会被整体剥离，防止说明文档中的示例块被误匹配。
    换行使用 \\r?\\n 以兼容 CRLF 来源（如 git 命令输出）。
    """
    # 剥离 HTML 注释，防止说明文档中的示例块被误匹配
    content = re.sub(r'<!--.*?-->', '', file_content, flags=re.DOTALL)
    block_re = re.compile(r'---target:\s*(.+?)---[ \t]*\r?\n(.*?)---end---', re.DOTALL | re.IGNORECASE)
    matched = []
    for m in block_re.finditer(content):
        targets = [t.strip().lower() for t in m.group(1).split(',')]
        block = m.group(2).strip()
        if block and ('all' in targets or tag_type in targets):
            matched.append(block)
    return '\n\n'.join(matched)
