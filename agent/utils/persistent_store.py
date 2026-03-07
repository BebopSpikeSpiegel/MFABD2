import json
import os
import shutil
import platform
from pathlib import Path
from . import mfaalog as logger

# ==============================================================================
# 🛠️ 存档系统使用指南 (PersistentStore Usage) - 双轨制加强版
# ==============================================================================
# 特性：
# 1. 智能路径: 优先全局系统目录 (MFABD2)，检测到根目录存档则自动切为绿色模式
# 2. 原子写入: 防止断电导致文件损坏 (.tmp 机制)
# 3. 自动备份: 每次写入自动生成 .bak 备份
# 4. 自动恢复: 主文件损坏时尝试从备份恢复
# ==============================================================================

class PersistentStore:
    # 核心名称配置
    APP_NAME = "MFABD2"
    FILE_NAME = "agent_save_data.json"
    BAK_NAME = "agent_save_data.json.bak"
    
    # 状态与路径变量 (延迟初始化)
    _initialized = False
    _mode = None
    CONFIG_DIR = None
    FILE_PATH = None
    BACKUP_PATH = None

    @classmethod
    def _init_paths(cls):
        """核心：环境探测与路径分配 (仅运行一次)"""
        if cls._initialized:
            return
            
        # 获取项目根目录 (根据你的设定，退回3层)
        base_dir = Path(__file__).resolve().parent.parent.parent
        
        # 1. 检查绿色模式触发条件 (根目录下存在存档或备份文件)
        portable_file = base_dir / cls.FILE_NAME
        portable_bak = base_dir / cls.BAK_NAME
        
        if portable_file.exists() or portable_bak.exists():
            cls._set_portable_mode(base_dir)
        else:
            # 2. 尝试进入全局模式 (并进行权限测试)
            if not cls._try_set_global_mode():
                logger.warning("[Py] ⚠️ 全局目录读写测试失败，自动降级为【绿色便携模式】。")
                cls._set_portable_mode(base_dir)
                
        cls._initialized = True
        
        # 状态汇报 (对应 main.py 的启动输出)
        mode_str = "系统全局模式" if cls._mode == 'global' else "绿色便携模式"
        logger.info(f"[Py] 💾 存档管理已挂载 | 模式: {mode_str}")
        logger.info(f"[Py] 📂 存档路径: {cls.FILE_PATH}")

    @classmethod
    def _set_portable_mode(cls, base_dir: Path):
        """设定为绿色便携模式"""
        cls._mode = 'portable'
        cls.CONFIG_DIR = base_dir
        cls.FILE_PATH = cls.CONFIG_DIR / cls.FILE_NAME
        cls.BACKUP_PATH = cls.CONFIG_DIR / cls.BAK_NAME

    @classmethod
    def _try_set_global_mode(cls) -> bool:
        """尝试设定全局模式，并测试读写权限"""
        system = platform.system()
        if system == "Windows":
            sys_dir = os.getenv("APPDATA") or os.path.expanduser("~")
        elif system == "Darwin":
            sys_dir = os.path.expanduser("~/Library/Application Support")
        else:
            sys_dir = os.getenv("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
            
        config_dir = Path(sys_dir) / cls.APP_NAME
        
        try:
            # 测试创建目录
            config_dir.mkdir(parents=True, exist_ok=True)
            # 测试写入与删除权限
            test_file = config_dir / ".rw_test.tmp"
            with open(test_file, 'w', encoding='utf-8') as f:
                f.write("test")
            test_file.unlink() # 等同于 os.remove
            
            # 测试通过
            cls._mode = 'global'
            cls.CONFIG_DIR = config_dir
            cls.FILE_PATH = cls.CONFIG_DIR / cls.FILE_NAME
            cls.BACKUP_PATH = cls.CONFIG_DIR / cls.BAK_NAME
            return True
        except Exception as e:
            logger.error(f"[Py] ❌ 系统目录访问异常: {e}")
            return False

    @classmethod
    def load(cls) -> dict:
        """【智能读取】优先读主文件，坏了读备份，再坏了才重置"""
        cls._init_paths()

        # 类型收窄 (Type Narrowing)：告诉静态检查器这些变量到这里绝对不是 None
        assert cls.FILE_PATH is not None
        assert cls.BACKUP_PATH is not None
        
        # 0：如果主文件不存在，但备份文件存在，先尝试恢复备份
        if not cls.FILE_PATH.exists() and cls.BACKUP_PATH.exists():
             
             try:
                 shutil.copy2(cls.BACKUP_PATH, cls.FILE_PATH)
                 logger.info("[Py] ✅ 已从备份自动生成主存档！")
             except Exception as e:
                 logger.error(f"[Py] ❌ 恢复备份失败: {e}")

        # 1. 尝试读取主文件
        data = cls._try_load_file(cls.FILE_PATH)
        if data is not None:
            return data
            
        logger.warning(f"[Py] ⚠️ 主存档损坏: {cls.FILE_PATH}")
        
        # 2. 主文件坏了，尝试读取备份文件
        if cls.BACKUP_PATH.exists():
            logger.info(f"[Py] 🔄 正在尝试从备份恢复: {cls.BACKUP_PATH}")
            data = cls._try_load_file(cls.BACKUP_PATH)
            if data is not None:
                logger.info("[Py] ✅ 备份恢复成功！")
                cls._save_file(cls.FILE_PATH, data) # 修复主文件
                return data

        # 3. 没救了，重置
        logger.error("[Py] ❌ 存档彻底损坏且无有效备份，重置为空。")
        empty_data = {}
        cls._save_file(cls.FILE_PATH, empty_data)
        return empty_data

    @classmethod
    def _try_load_file(cls, path: Path) -> dict | None:
        """底层读取逻辑"""
        if not path.exists():
            return None  # ✅ 修改点1：不存在应视为失败，交给外层处理
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # ✅ 修改点2：严格校验必须是字典结构
            if not isinstance(data, dict):
                logger.error(f"[Py] ❌ 存档内容不是有效的字典结构: {path}")
                return None
            return data
        except Exception:
            return None

    @classmethod
    def save(cls, data: dict):
        """【安全写入】写主文件 -> 成功 -> 覆盖备份"""
        cls._init_paths()

        # ✅ 类型收窄，消除 shutil.copy2 的类型警告
        assert cls.FILE_PATH is not None
        assert cls.BACKUP_PATH is not None
        
        if cls._save_file(cls.FILE_PATH, data):
            try:
                shutil.copy2(cls.FILE_PATH, cls.BACKUP_PATH)
            except Exception as e:
                logger.warning(f"[Py] 备份更新失败 (不影响主流程): {e}")

    @classmethod
    def _save_file(cls, path: Path, data: dict) -> bool:
        """底层写入逻辑 (原子级)"""
        try:
            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            shutil.move(tmp_path, path)
            return True
        except Exception as e:
            logger.error(f"[Py] 写入文件失败 {path}: {e}")
            return False

    @classmethod
    def get(cls, key: str, default=None):
        data = cls.load()
        return data.get(key, default)

    @classmethod
    def set(cls, key: str, value):
        data = cls.load()
        data[key] = value
        cls.save(data)