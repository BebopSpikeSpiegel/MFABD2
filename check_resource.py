import sys
from typing import List, Dict
from pathlib import Path

try:
    import jsonc
except ImportError:
    print("❌ 缺少依赖: json-with-comments")
    print("请运行: pip install json-with-comments")
    sys.exit(1)

from maa.resource import Resource
from maa.tasker import Tasker, LoggingLevelEnum


def load_combinations(assets_dir: Path) -> List[Dict]:
    """从 interface.json 的 resource 字段提取有序资源加载组合"""
    interface_json = assets_dir / "interface.json"
    if not interface_json.exists():
        print(f"❌ 未找到 interface.json: {interface_json}")
        sys.exit(1)

    with open(interface_json, "r", encoding="utf-8") as f:
        data = jsonc.load(f)

    seen = set()
    combinations = []
    for entry in data.get("resource", []):
        paths = tuple(entry.get("path", []))
        if paths in seen:
            continue
        seen.add(paths)
        resolved = [(assets_dir / p).resolve() for p in paths]
        name = entry.get("name", " + ".join(paths))
        combinations.append({"name": name, "paths": resolved})

    return combinations


def check_combination(name: str, paths: List[Path]) -> bool:
    """对单个资源组合按声明顺序加载并检查"""
    print(f"\n🔍 检查组合: [{name}]  ({' -> '.join(p.name for p in paths)})")
    resource = Resource()
    for path in paths:
        print(f"  -> 加载: {path}")
        status = resource.post_bundle(str(path)).wait().status
        if not status.succeeded:
            print(f"  ❌ 加载失败: {path}")
            return False
        print(f"  ✅ 加载成功: {path}")
    print(f"✅ 组合 [{name}] 检查通过")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python check_resource.py <assets_dir>")
        print("  assets_dir: 包含 interface.json 的目录（如 ./assets/）")
        sys.exit(1)

    Tasker.set_stdout_level(LoggingLevelEnum.All)

    assets_dir = Path(sys.argv[1]).resolve()
    combinations = load_combinations(assets_dir)

    if not combinations:
        print("❌ interface.json 中未找到任何 resource 组合")
        sys.exit(1)

    print(f"🚀 从 interface.json 提取到 {len(combinations)} 个资源组合，开始检查...")

    all_passed = True
    for combo in combinations:
        if not check_combination(combo["name"], combo["paths"]):
            all_passed = False

    if not all_passed:
        sys.exit(1)

    print("\n🎉 所有资源组合检查完毕，未发现错误。")


if __name__ == "__main__":
    main()
