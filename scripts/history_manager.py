#!/usr/bin/env python3
"""
历史版本管理模块
"""

import os
import re
import requests
from typing import List, Dict, Optional
from version_rules import filter_valid_versions, sort_versions, is_valid_formal_version

class HistoryManager:
    def __init__(self, github_token: str, repo_owner: str, repo_name: str):
        self.github_token = github_token
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.base_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}"
        self.headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "MFABD2-History-Manager"
        }

    def parse_version(self, tag: str) -> tuple:
        """解析版本号，返回 (major, minor, patch)。
        直接捕获 vMAJOR.MINOR.PATCH 前缀，后缀类型（-beta/-alpha/-ci 等）无需枚举。
        ⚠️ 若版本号格式（如前缀 'v'、分隔符）发生变更，需同步修改此处正则。
        """
        # 匹配示例：v2.3.7 / v2.3.7-beta.251112.cf64235 / v2.3.7-alpha.260124.cf64235
        match = re.match(r'^v(\d+)\.(\d+)\.(\d+)', tag)
        if not match:
            raise ValueError(f"版本格式异常: {tag}")
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    
    def fetch_all_releases(self) -> List[Dict]:
        """获取所有releases，失败则抛出 RuntimeError（由上层捕获）"""
        print("获取GitHub Releases...")
        url = f"{self.base_url}/releases"
        releases = []
        page = 1
        
        try:
            while True:
                response = requests.get(f"{url}?page={page}&per_page=100", headers=self.headers, timeout=30)
                if response.status_code != 200:
                    raise Exception(f"API请求失败: {response.status_code} - {response.text}")
                
                page_releases = response.json()
                if not page_releases:
                    break
                    
                releases.extend(page_releases)
                page += 1
                
                if page > 10:  # 安全限制
                    print("警告: 达到页面限制，停止获取更多releases")
                    break
            
            print(f"成功获取 {len(releases)} 个releases")
            return releases
            
        except Exception as e:
            raise RuntimeError(f"获取Releases失败: {e}") from e
    
    def get_minor_version_series(self, current_tag: str) -> List[Dict]:
        """获取同次版本的所有正式版Release"""
        try:
            current_major, current_minor, _ = self.parse_version(current_tag)
            print(f"当前版本: v{current_major}.{current_minor}.x 系列")
        except ValueError:
            # 如果版本解析失败（比如当前是公测版），使用最新正式版作为基准
            print(f"当前标签 {current_tag} 不是正式版，使用最新正式版作为历史基准")
            all_releases = self.fetch_all_releases()
            formal_releases = [r for r in all_releases if is_valid_formal_version(r['tag_name'])]
            if formal_releases:
                latest_formal = max(formal_releases, key=lambda r: self.parse_version(r['tag_name']))
                current_major, current_minor, _ = self.parse_version(latest_formal['tag_name'])
                print(f"使用基准版本: v{current_major}.{current_minor}.x 系列")
            else:
                print("没有找到任何正式版，跳过历史版本")
                return []
        
        all_releases = self.fetch_all_releases()
        
        relevant_releases = []
        for release in all_releases:
            tag = release['tag_name']
            if not is_valid_formal_version(tag):
                continue
                
            try:
                major, minor, _ = self.parse_version(tag)
                # 只包含完全相同的次版本，不包含更早的
                if major == current_major and minor == current_minor:
                    # 排除当前版本自身（如果是正式版）
                    if tag != current_tag:
                        relevant_releases.append(release)
                        print(f"包含历史版本: {tag}")
                    else:
                        print(f"排除当前版本: {tag}")
                else:
                    print(f"跳过不同次版本: {tag} (当前: v{current_major}.{current_minor}.x)")
            except ValueError:
                # 跳过解析失败的版本
                print(f"跳过解析失败版本: {tag}")
                continue
        
        # 按版本号排序（从新到旧）
        relevant_releases.sort(key=lambda r: self.parse_version(r['tag_name']), reverse=True)
        
        print(f"找到 {len(relevant_releases)} 个相关历史版本")
        return relevant_releases
    
    def truncate_release_body(self, body: str) -> str:
        """截断Release正文 - 修复版：基于CDK链接和构建信息"""
        if not body:
            return ""
        
        body = body.strip()
        
        # 第一优先级：CDK链接（用户内容结束标志）
        cdk_patterns = [
            r'\[已有 Mirror酱 CDK[^\]]*\]\([^)]+\)',
            r'\[Mirror酱 CDK[^\]]*\]\([^)]+\)',
        ]
        
        for pattern in cdk_patterns:
            cdk_match = re.search(pattern, body)
            if cdk_match:
                truncated = body[:cdk_match.start()].strip()
                if not truncated:
                    print(f"⚠️ CDK链接截断后内容为空（CDK位于body开头？），跳过此锚点，继续尝试其他截断方式")
                    break
                print(f"使用CDK链接截断，长度: {len(truncated)}")
                return truncated
        
        # 第二优先级：构建信息（自动化内容开始）
        build_info_match = re.search(r'\*\*构建信息\*\*:', body)
        if build_info_match:
            truncated = body[:build_info_match.start()].strip()
            print(f"使用构建信息截断，长度: {len(truncated)}")
            return truncated
        
        # 第三优先级：历史版本标记（历史内容开始）
        historical_marker = "## 历史版本更新内容"
        marker_pos = body.find(historical_marker)
        if marker_pos != -1:
            truncated = body[:marker_pos].strip()
            print(f"使用历史版本标记截断，长度: {len(truncated)}")
            return truncated
        
        # 保底：返回完整内容
        print(f"使用完整内容，长度: {len(body)}")
        return body
    
    def remove_duplicate_cdk_links(self, body: str) -> str:
        """移除重复的CDK链接，只保留一个"""
        cdk_pattern = r'\[已有 Mirror酱 CDK[^\]]*\]\([^)]+\)'
        cdk_matches = list(re.finditer(cdk_pattern, body))
        
        if len(cdk_matches) <= 1:
            return body
        
        # 保留最后一个CDK链接
        last_cdk = cdk_matches[-1]
        result = body[:last_cdk.start()] + body[last_cdk.start():last_cdk.end()]
        
        return result.strip()
    
    def smart_length_truncate(self, body: str, max_lines: int = 50) -> str:
        """智能长度截断"""
        lines = body.split('\n')
        if len(lines) <= max_lines:
            return body
        
        # 找到合理的截断点（段落边界）
        for i in range(max_lines, 0, -1):
            if i < len(lines) and (lines[i].strip() == '' or lines[i].startswith('#')):
                return '\n'.join(lines[:i]).strip()
        
        # 实在找不到就在max_lines处硬截断
        return '\n'.join(lines[:max_lines]).strip() + "\n\n..."

def test_history_manager():
    """测试历史版本管理器"""
    print("=== 历史版本管理器测试 ===")
    
    # 需要设置环境变量
    token = os.environ.get('GITHUB_TOKEN')
    repo = os.environ.get('GITHUB_REPOSITORY')
    
    if not token or not repo:
        print("缺少环境变量，跳过测试")
        return
    
    repo_owner, repo_name = repo.split('/')
    manager = HistoryManager(token, repo_owner, repo_name)
    
    # 测试同次版本获取
    test_tag = "v2.3.6"
    historical_versions = manager.get_minor_version_series(test_tag)
    
    print(f"测试标签: {test_tag}")
    print(f"找到的历史版本: {[r['tag_name'] for r in historical_versions]}")

if __name__ == "__main__":
    test_history_manager()