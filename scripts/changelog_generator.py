#!/usr/bin/env python3
"""
主入口 - 协调整个变更日志生成流程
"""

import os
import pathlib
import sys
import re
from typing import List, Dict
from version_logic import calculate_compare_base
from git_operations import get_commit_list
from version_rules import filter_valid_versions, sort_versions
from history_manager import HistoryManager
from version_analyzer import analyze_version_highlights
from History_config import HISTORY_CONFIG, OUTPUT_CONFIG
from git_operations import get_commit_list, get_merge_commits, get_released_branches_from_main, safe_get_commit_list, ensure_reference_exists, get_commit_timestamp

def group_commits_by_type(commits: List[Dict]) -> Dict[str, List[Dict]]:
    """按提交类型分组（简化版本，后续可以改进）"""
    groups = {
        'feat': [],
        'fix': [], 
        'docs': [],
        'style': [],
        'refactor': [],
        'test': [],
        'chore': [],
        'impr': [],
        'perf': [],
        'build': [],
        'ci': [],
        'other': []
    }
    
    for commit in commits:
        subject = commit['subject'].lower()
        
        if subject.startswith('feat'):
            groups['feat'].append(commit)
        elif subject.startswith('fix'):
            groups['fix'].append(commit)
        elif subject.startswith('docs'):
            groups['docs'].append(commit)
        elif subject.startswith('style'):
            groups['style'].append(commit)
        elif subject.startswith('refactor'):
            groups['refactor'].append(commit)
        elif subject.startswith('test'):
            groups['test'].append(commit)
        elif subject.startswith('chore'):
            groups['chore'].append(commit)
        elif subject.startswith('impr'):
            groups['impr'].append(commit)
        elif subject.startswith('perf'):
            groups['perf'].append(commit)
        elif subject.startswith('build'):
            groups['build'].append(commit)
        elif subject.startswith('ci'):
            groups['ci'].append(commit)
        else:
            groups['other'].append(commit)
    
    return groups

def clean_commit_message(subject: str) -> str:
    """清理提交信息，移除类型前缀"""
    # 匹配模式：类型(作用域): 信息（支持中英文冒号）
    patterns = [
        r'^(feat|fix|docs|style|refactor|test|chore|impr|perf|build|ci|revert)(\(\w+\))?\s*[：:]\s*',  # 中英文冒号
        r'^(Feat|Fix|Docs|Style|Refactor|Test|Chore|Impr|Perf|Build|Ci|Revert)(\(\w+\))?\s*[：:]\s*',  # 首字母大写
        r'^(FEAT|FIX|DOCS|STYLE|REFACTOR|TEST|CHORE|IMPR|PERF|BUILD|CI|REVERT)(\(\w+\))?\s*[：:]\s*',  # 全大写
    ]
    
    for pattern in patterns:
        cleaned = re.sub(pattern, '', subject)
        if cleaned != subject:
            return cleaned
    
    return subject

def detect_commit_highlights(commit: Dict) -> Dict[str, bool]:
    """检测提交的特殊标记"""
    body = commit.get('body', '')
    subject = commit.get('subject', '')
    full_text = body + ' ' + subject
    
    return {
        'is_breaking': any(re.search(pattern, full_text, re.IGNORECASE) 
                          for pattern in [r'BREAKING CHANGE', r'BREAKING-CHANGE', r'^.*!:']),
        'is_highlight': 'HIGHLIGHT:' in body.upper()
    }

def detect_coauthors(body: str) -> List[str]:
    """检测提交信息中的协作者"""
    coauthors = []
    if not body:
        return coauthors
    
    # 匹配 Co-authored-by 格式
    coauthor_pattern = r'Co-authored-by:\s*([^<\n]+)(?:<[^>]+>)?'
    matches = re.findall(coauthor_pattern, body, re.IGNORECASE | re.MULTILINE)
    
    for match in matches:
        coauthor_name = match.strip()
        if coauthor_name:
            coauthors.append(f"👥{coauthor_name}")
    
    return coauthors

def format_commit_message(commit: Dict) -> str:
    """格式化单个提交信息，清理类型前缀"""
    subject = commit['subject']
    author = commit['author_name']
    body = commit.get('body', '')  # 获取提交正文
    
    # 清理提交信息（移除类型前缀）
    cleaned_subject = clean_commit_message(subject)

    # 检测特殊标记
    highlights = detect_commit_highlights(commit)
    breaking_marker = "⚠️ [破坏性变更] " if highlights['is_breaking'] else ""
    highlight_marker = "💡 " if highlights['is_highlight'] else ""

    # 检测是否为机器人账号（根据配置决定是否显示）
    is_bot = '[bot]' in author.lower()
    if HISTORY_CONFIG['show_bot_accounts'] and is_bot:
        author_display = f"{author} 🤖"
    else:
        author_display = author

    # 检测协作者信息
    coauthors = detect_coauthors(body)
    if coauthors and HISTORY_CONFIG['coauthor_display']:
        coauthor_suffix = " " + " ".join(coauthors)
        author_display += coauthor_suffix

    return f"- {breaking_marker}{highlight_marker}{cleaned_subject} @{author_display}"

def parse_merge_subject(subject: str) -> tuple:
    """解析合并提交标题，返回 (分支名, 描述)"""
    # 1. 优先尝试新格式
    pattern_new = r"^Merge:'([^']+)'\|\s*(.+)"
    match = re.search(pattern_new, subject)
    if match:
        return match.group(1), match.group(2).strip()
        
    # 2. 兼容 Git 默认格式 (防止旧合并丢失)
    pattern_old = r"Merge branch '([^']+)'"
    match = re.search(pattern_old, subject)
    if match:
        branch_name = match.group(1)
        # 简单生成描述
        desc = f"合并分支 {branch_name}"
        return branch_name, desc
        
    return None, None

def get_beta_preview_content(compare_base: str, current_tag: str) -> str:
    """生成 Beta 功能预览板块"""
    # 标签不存在时的自动回退
    target_ref = current_tag
    if not ensure_reference_exists(target_ref):
        print(f"Beta预览: 引用 {target_ref} 不存在，自动回退到 HEAD")
        target_ref = "HEAD"
        
    # 在获取 merges 之前，先获取基准版本的时间戳
    base_ts = get_commit_timestamp(compare_base)
    print(f"时间过滤基准: {compare_base} (TS: {base_ts})")
    
    # 获取区间内的合并提交
    merges = get_merge_commits(compare_base, target_ref)
    if not merges:
        return ""
        
    # 获取 Main 分支已发布的功能黑名单
    # 如果是公测版/内测版/CI版 -> 过滤基准是 "main" (隐藏已正式发布的功能)
    # 如果是正式版     -> 过滤基准是 compare_base (隐藏上个版本以前的功能)
    # 修改点：加入 -alpha 判断
    is_beta_or_ci = '-beta' in current_tag or '-ci' in current_tag or '-alpha' in current_tag
    
    if is_beta_or_ci:
        filter_ref = "main"
    else:
        # 再次检查 compare_base 是否存在，不存在也回退
        filter_ref = compare_base if ensure_reference_exists(compare_base) else "HEAD"
        
    print(f"Beta预览过滤基准: {filter_ref}")
    released_branches = get_released_branches_from_main(ref=filter_ref)
    
    active_features = {} # {branch_name: description}
    
    # 定义反向合并的关键词前缀
    IGNORE_PREFIXES = ['main', 'master', 'develop', 'release']

    for commit in merges:
        # 【新增】时间过滤逻辑
        # 如果该合并发生的时间 早于 正式版发布时间，说明它是“陈年旧账”，直接跳过
        if base_ts > 0 and commit['timestamp'] < base_ts:
            # print(f"跳过旧合并: {commit['subject']} (早于基准)")
            continue
        
        branch, desc = parse_merge_subject(commit['subject'])
        
        if branch:
            branch_lower = branch.lower()
            
            # 过滤1: 忽略反向合并 (前缀匹配)
            if any(branch_lower.startswith(prefix) for prefix in IGNORE_PREFIXES):
                continue
            # 过滤2: 已发布则跳过 (自动消失逻辑)
            if branch in released_branches:
                continue
            # 过滤3: 只保留最新的 (去重逻辑)
            if branch not in active_features:
                active_features[branch] = desc
    
    if not active_features:
        return ""
        
    lines = []
    
    # 修改点：只要是 beta/ci/alpha 都使用这套文案，不做动态替换
    if is_beta_or_ci:
        # 🧪 公测版/开发版文案
        lines.append("### 🧬 正在测试的功能 (Beta Preview)")
        lines.append("> 遇到问题请及时在 [Issue](https://github.com/sunyink/MFABD2/issues) 中反馈，有助于早日形成可靠的稳定版。")
        lines.append("") # 制造一个空行，隔开列表
        lines.append("> 下列功能已合并入测试版，重点关注是否存在Bug：")
    else:
        # 🚀 正式版文案 (方案B)
        lines.append("### 🚀 正式版-版本功能概览 (Feature Branches)")
        lines.append("> 感谢参与`公测版`开发的各位，本次`正式版`更新包含以下‘转录’的功能分支：")

    lines.append("") # 制造一个空行，隔开列表

    for branch, desc in active_features.items():
        lines.append(f"- {desc} `({branch})`")
    
    lines.append("") # 结尾空行
    return "\n".join(lines)

def generate_changelog_content(commits: List[Dict], current_tag: str, compare_base: str) -> str:
    """生成变更日志内容"""
    
    if not commits:
        return f"# 更新日志\n\n## {current_tag}\n\n*无显著变更*\n"
    
    # 目的：过滤掉标题完全相同的提交（保留最新的那一个）
    unique_commits = []
    seen_subjects = set()
    
    # 此时 commits 列表通常是按时间倒序（最新的在前），所以保留第一次遇到的即可
    for commit in commits:
        # 去除首尾空格，并不区分大小写（可选）来判断重复
        subject = commit['subject'].strip()
        
        if subject not in seen_subjects:
            seen_subjects.add(subject)
            unique_commits.append(commit)
        else:
            # 在控制台打印被过滤的提交，方便调试
            print(f"过滤重复提交: {subject} ({commit['hash'][:7]})")
            
    # 将去重后的列表赋值回 commits
    commits = unique_commits
    
    grouped_commits = group_commits_by_type(commits)
    
    # 构建变更日志
    changelog = f"# 更新日志\n\n"
    changelog += f"## {current_tag}\n\n"

    # 读取 Release 头部草稿 (draft_release_header.md)
    draft_header_path = os.path.join(os.path.dirname(__file__), 'draft_release_header.md')
    if os.path.exists(draft_header_path):
        try:
            with open(draft_header_path, 'r', encoding='utf-8') as f:
                header_content = f.read().strip()
                if header_content:
                    print(f"📖 发现发布草稿，已插入 Release 头部: {draft_header_path}")
                    changelog += header_content + "\n\n---\n\n" # 加上分隔符和换行
        except Exception as e:
            print(f"⚠️ 读取发布草稿失败: {e}")

    try:
        changelog += get_beta_preview_content(compare_base, current_tag)
    except Exception as e:
        print(f"Beta预览生成忽略错误: {e}")
    grouped_commits = group_commits_by_type(commits)
    # 定义分组标题
    group_titles = {
        'feat': '✨ 新功能',
        'fix': '🐛 Bug修复', 
        'docs': '📚 文档',
        'style': '🎨 样式',
        'refactor': '🚜 代码重构',
        'test': '🧪 测试',
        'chore': '🔧 日常维护',
        'impr': '💪 功能增强',
        'perf': '🚀 性能优化',
        'build': '🔨 构建维护',
        'ci': '⚙️ CI配置',
        'other': '其他变更'
    }
    
    # 输出有内容的分组
    for group_type, title in group_titles.items():
        group_commits = grouped_commits[group_type]
        if group_commits:
            filtered_commits = [
                c for c in group_commits 
                if not c['subject'].startswith("Merge:'")
            ]
            
            if filtered_commits:
                changelog += f"### {title}\n\n"
                for commit in filtered_commits:
                    changelog += format_commit_message(commit) + "\n"
            changelog += "\n"
    
    changelog += "[已有 Mirror酱 CDK？前往 Mirror酱 高速下载](https://mirrorchyan.com/zh/projects?rid=MFABD2)\n\n"

    changelog += f"**对比范围**: {compare_base} → {current_tag}\n\n"

    # 构建信息放在这里（历史版本前面）
    changelog += "**构建信息**:\n"
    
    # 动态获取版本类型
    if '-beta' in current_tag:
        version_type = "公测版"
    elif '-alpha' in current_tag: # 修改点：新增内测版
        version_type = "内测版"
    elif '-ci' in current_tag:
        version_type = "开发版"
    else:
        version_type = "正式版"
    
    changelog += f"- 版本: `{current_tag}`\n"
    changelog += f"- 类型: {version_type}\n"
    changelog += f"- 分支: {os.environ.get('GITHUB_REF_NAME', '未知')}\n"
    
    # 使用当前时间作为构建时间
    from datetime import datetime
    build_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    changelog += f"- 构建时间: {build_time}\n\n"


    return changelog

def add_historical_versions(current_changelog: str, current_tag: str) -> str:
    """添加历史版本折叠内容"""
    print("准备获取历史版本...")
    print(f"当前标签: {current_tag}")
    
    # 获取环境变量
    github_token = os.environ.get('GITHUB_TOKEN')
    github_repository = os.environ.get('GITHUB_REPOSITORY')
    
    print(f"GITHUB_TOKEN: {'已设置' if github_token else '未设置'}")
    print(f"GITHUB_REPOSITORY: {github_repository}")
    
    if not github_token or not github_repository:
        print("缺少GitHub环境变量，跳过历史版本")
        return current_changelog
    
    try:
        repo_owner, repo_name = github_repository.split('/')
        manager = HistoryManager(github_token, repo_owner, repo_name)
        
        # 获取同次版本的历史Release
        historical_releases = manager.get_minor_version_series(current_tag)
        
        if not historical_releases:
            print("没有找到相关历史版本")
            return current_changelog
        
        # 构建历史版本折叠内容
        historical_section = "\n## 历史版本更新内容\n\n"
        
        for release in historical_releases:
            tag = release['tag_name']
            published_at = release.get('published_at', '')[:10] if release.get('published_at') else "未知日期"
            body = release.get('body', '') or ""
            
            print(f"处理历史版本: {tag} (发布时间: {published_at})")
            print(f"内容长度: {len(body)} 字符")
            
            # 智能标记分析（根据配置决定是否启用）
            markers = ""
            if HISTORY_CONFIG['enable_version_highlights']:
                markers = analyze_version_highlights(release)
                marker_display = f" {markers}" if markers else ""
                print(f"版本标记: '{markers}'")
            else:
                marker_display = ""
                print("版本标记: 已禁用")
            
            # 截断处理
            truncated_body = manager.truncate_release_body(body)
            print(f"截断后长度: {len(truncated_body)} 字符")
            
            if not truncated_body.strip():
                print(f"跳过版本 {tag}: 内容为空")
                continue
            
            # 检查内容是否与其他版本重复
            body_hash = hash(truncated_body.strip())
            print(f"内容哈希: {body_hash}")
            
            historical_section += f"""<details>
<summary>{tag} ({published_at}){marker_display}</summary>

{truncated_body}

</details>

"""
        
        print(f"成功添加 {len(historical_releases)} 个历史版本")
        
        # 添加历史版本结束标识
        if historical_releases:
            historical_section += "---\n*以上为历史版本信息*\n\n"
        
        return current_changelog + historical_section
        
    except Exception as e:
        print(f"❌ 历史版本处理失败: {e}")
        # 不终止作业，返回原始内容
        return current_changelog

def main():
    """主函数"""
    print("=== 变更日志生成器 ===\n")
    
    # 获取当前标签（从环境变量或参数）
    current_tag = os.environ.get('CURRENT_TAG')
    if not current_tag:
        # 如果没有环境变量，使用测试标签
        current_tag = "v2.3.5"
        print(f"使用测试标签: {current_tag}")
    else:
        print(f"使用环境变量标签: {current_tag}")
    
    # 计算对比基准
    print("计算对比基准...")
    compare_base = calculate_compare_base(current_tag)
    print(f"对比基准: {compare_base}")
    
    # 获取提交列表（使用安全版本）
    print("获取提交列表...")
    from git_operations import safe_get_commit_list
    commits = safe_get_commit_list(compare_base, current_tag)
    print(f"获取到 {len(commits)} 个提交")
    
    # 生成基础变更日志
    print("生成基础变更日志...")
    changelog_content = generate_changelog_content(commits, current_tag, compare_base)
    
    # 添加历史版本内容
    print("添加历史版本...")
    changelog_content = add_historical_versions(changelog_content, current_tag)
    
    # 输出到文件
    output_file = "../CHANGES.md"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(changelog_content)
    
    print(f"✅ 变更日志已生成: {output_file}")
    
    # 显示预览
    print("\n=== 变更日志预览 ===")
    lines = changelog_content.split('\n')
    for line in lines[:20]:  # 显示前20行
        print(line)
    
    if len(lines) > 20:
        print("... (完整内容请查看 CHANGES.md 文件)")

def test_changelog_generator():
    """测试变更日志生成器"""
    print("=== 变更日志生成器测试 ===\n")
    
    test_cases = [
        "v2.3.5",      # 正式版
        "v2.3.4",      # 另一个正式版
    ]
    
    for test_tag in test_cases:
        print(f"测试标签: {test_tag}")
        print("-" * 40)
        
        compare_base = calculate_compare_base(test_tag)
        commits = get_commit_list(compare_base, test_tag)
        
        print(f"对比基准: {compare_base}")
        print(f"提交数量: {len(commits)}")
        print()

if __name__ == "__main__":
    # 测试模式
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_changelog_generator()
    else:
        # 正常模式
        main()