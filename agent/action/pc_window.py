"""PC端窗口管理 Custom Action

调整棕色尘埃2 PC客户端窗口为 1280x720 窗口化模式，
以匹配 MaaFramework display_short_side=720 的坐标基准。

仅在 Windows 平台有效，其他平台直接跳过。
"""

import sys
import time
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

# 目标客户区尺寸（游戏画面，不含标题栏/边框）
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720

# 游戏窗口类名（Win32 controller 里配置的 class_regex）
WINDOW_CLASS = "UnityWndClass"


def _find_game_hwnd():
    """枚举顶层窗口，返回首个类名匹配且可见的游戏窗口 hwnd（找不到返回 HWND(0)，falsy）。

    仅 win32；供窗口调整/关闭等动作共享，避免各处各写一份 Unity 窗口枚举而走样。
    """
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    found = ctypes.wintypes.HWND(0)

    def enum_callback(hwnd, lparam):
        nonlocal found
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value == WINDOW_CLASS and user32.IsWindowVisible(hwnd):
            found = hwnd
            return False  # 停止枚举
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
    return found


def _find_and_resize_window() -> tuple[bool, str]:
    """
    查找游戏窗口并调整客户区到 1280x720。

    Returns:
        (success: bool, message: str)
    """
    if sys.platform != "win32":
        return True, "非Windows平台，跳过窗口调整"

    try:
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32

        hwnd = _find_game_hwnd()
        if not hwnd:
            return False, f"未找到类名为 '{WINDOW_CLASS}' 的游戏窗口，请先启动游戏"

        # 获取窗口标题用于日志
        title_buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title_buf, 256)
        title = title_buf.value

        # 获取当前窗口矩形（含边框）
        window_rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(window_rect))

        # 获取当前客户区矩形
        client_rect = ctypes.wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(client_rect))

        current_client_w = client_rect.right - client_rect.left
        current_client_h = client_rect.bottom - client_rect.top

        if current_client_w == TARGET_WIDTH and current_client_h == TARGET_HEIGHT:
            return True, f"窗口 '{title}' 已是 {TARGET_WIDTH}x{TARGET_HEIGHT}，无需调整"

        # 计算边框大小 = 窗口总大小 - 客户区大小
        window_w = window_rect.right - window_rect.left
        window_h = window_rect.bottom - window_rect.top
        border_w = window_w - current_client_w
        border_h = window_h - current_client_h

        # 目标窗口总大小 = 目标客户区 + 边框
        target_window_w = TARGET_WIDTH + border_w
        target_window_h = TARGET_HEIGHT + border_h

        # 先确保窗口不是最大化/最小化状态
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.1)

        # 调整窗口大小，保持当前位置
        SWP_NOMOVE = 0x0002
        SWP_NOZORDER = 0x0004
        SWP_FRAMECHANGED = 0x0020
        flags = SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED
        result = user32.SetWindowPos(hwnd, 0, 0, 0, target_window_w, target_window_h, flags)

        if not result:
            err = ctypes.GetLastError()
            return False, f"SetWindowPos 失败，错误码: {err}"

        # 验证调整结果
        time.sleep(0.2)
        user32.GetClientRect(hwnd, ctypes.byref(client_rect))
        actual_w = client_rect.right - client_rect.left
        actual_h = client_rect.bottom - client_rect.top

        if actual_w == TARGET_WIDTH and actual_h == TARGET_HEIGHT:
            return True, f"窗口 '{title}' 已调整为 {TARGET_WIDTH}x{TARGET_HEIGHT}"
        else:
            return False, (
                f"调整后客户区为 {actual_w}x{actual_h}，未达到目标 {TARGET_WIDTH}x{TARGET_HEIGHT}。"
                f"游戏可能锁定了分辨率，请在游戏内设置中手动调整为 FHD(1280x720) 窗口化。"
            )

    except ImportError as e:
        return False, f"缺少依赖: {e}"
    except Exception as e:
        return False, f"调整窗口时发生异常: {e}"


@AgentServer.custom_action("PC_ResizeWindow")
class PC_ResizeWindow(CustomAction):
    """
    调整游戏窗口客户区到 1280x720。

    pipeline 用法:
        {
            "action": "Custom",
            "custom_action": "PC_ResizeWindow"
        }

    成功时继续执行 next，失败时走 on_error。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        success, message = _find_and_resize_window()
        if success:
            print(f"[PC_ResizeWindow] ✅ {message}")
        else:
            print(f"[PC_ResizeWindow] ❌ {message}")
        return success


def _find_and_close_window() -> tuple[bool, str]:
    """
    查找棕色尘埃2 PC客户端窗口并关闭。

    ADB 的 StopApp 在 Win32 控制器无对应动作，这里用窗口/进程 API 替代：
    优先 WM_CLOSE 优雅关闭，2.5s 未消失则按 PID 强杀（对齐 StopApp 硬关语义）。

    Returns:
        (success: bool, message: str)
    """
    if sys.platform != "win32":
        return True, "非Windows平台，跳过关闭游戏"

    try:
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 显式声明返回/参数类型，避免 64 位下句柄被截断
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD
        ]
        kernel32.TerminateProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)
        ]

        hwnd = _find_game_hwnd()
        if not hwnd:
            return True, f"未找到 '{WINDOW_CLASS}' 游戏窗口，游戏可能已关闭，跳过"

        title_buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title_buf, 256)
        title = title_buf.value

        # 1) 优雅关闭：发 WM_CLOSE 后轮询窗口是否消失（秒关早退，最长 2.5s，不做固定阻塞）
        WM_CLOSE = 0x0010
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        deadline = time.time() + 2.5
        while time.time() < deadline:
            time.sleep(0.1)
            if not _find_game_hwnd():
                return True, f"窗口 '{title}' 已通过 WM_CLOSE 关闭"

        # 2) 强杀兜底
        pid = ctypes.wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False, f"窗口 '{title}' 未响应 WM_CLOSE 且拿不到 PID，无法强关"

        PROCESS_TERMINATE = 0x0001
        h_proc = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid.value)
        if not h_proc:
            err = ctypes.GetLastError()
            return False, f"OpenProcess 失败(pid={pid.value}, err={err})"
        try:
            ok = kernel32.TerminateProcess(h_proc, 0)
        finally:
            kernel32.CloseHandle(h_proc)
        if not ok:
            err = ctypes.GetLastError()
            return False, f"TerminateProcess 失败(pid={pid.value}, err={err})"

        time.sleep(0.5)
        return True, f"窗口 '{title}' 未响应 WM_CLOSE，已强制结束进程(pid={pid.value})"

    except ImportError as e:
        return False, f"缺少依赖: {e}"
    except Exception as e:
        return False, f"关闭游戏窗口时发生异常: {e}"


@AgentServer.custom_action("PC_StopApp")
class PC_StopApp(CustomAction):
    """
    关闭棕色尘埃2 PC客户端窗口（替代 ADB StopApp）。

    pipeline 用法:
        {
            "action": "Custom",
            "custom_action": "PC_StopApp"
        }

    优雅 WM_CLOSE 优先，2.5s 未关则按 PID 强杀；游戏已关时 no-op 成功。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        success, message = _find_and_close_window()
        if success:
            print(f"[PC_StopApp] ✅ {message}")
        else:
            print(f"[PC_StopApp] ❌ {message}")
        return success
