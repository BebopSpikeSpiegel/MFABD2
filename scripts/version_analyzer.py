#!/usr/bin/env python3
"""
版本分析模块 - 智能标记检测
"""

import re

def analyze_version_highlights(body: str) -> str:
    """分析版本的亮点标记"""
    markers = []
    if contains_breaking_change(body):
        markers.append('⚠️')
    if contains_highlight_feature(body):
        markers.append('💡')
    
    return ''.join(markers)

def contains_breaking_change(text: str) -> bool:
    """检测是否包含破坏性变更"""
    if not text:
        return False
        
    patterns = [r'⚠️', r'破坏性变更', r'BREAKING CHANGE', r'BREAKING-CHANGE']
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)

def contains_highlight_feature(text: str) -> bool:
    """检测是否包含亮点功能"""
    if not text:
        return False
        
    patterns = [r'💡', r'HIGHLIGHT', r'重要更新', r'亮点功能', r'重大更新']
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)

def test_analyzer():
    """测试分析器"""
    test_cases = [
        {"body": "这个版本有⚠️破坏性变更"},
        {"body": "HIGHLIGHT: 重要新功能"},
        {"body": "普通更新"},
        {"body": "既有⚠️又有💡"},
    ]
    
    print("=== 版本分析器测试 ===")
    for i, test_case in enumerate(test_cases, 1):
        markers = analyze_version_highlights(test_case['body'])
        print(f"测试 {i}: '{test_case['body'][:20]}...' → 标记: '{markers}'")

if __name__ == "__main__":
    test_analyzer()