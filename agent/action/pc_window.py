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

from utils import mfaalog

# 目标客户区尺寸（游戏画面，不含标题栏/边框）
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720

# 调整采用"测量→按实测差值补偿→再测量"的闭环，不做一次性边框推算。
# 差值补偿通常 1~2 轮到位，余量留给 DPI 取整、DWM 不可见边框，以及
# Unity 在 resize 之后自己把窗口改回去的情况。
MAX_RESIZE_ATTEMPTS = 5
SETTLE_DELAY = 0.15  # SetWindowPos 之后等窗口消息处理完
STABILIZE_DELAY = 0.4  # 达标后再复查一次，看游戏会不会把窗口改回去
FULLSCREEN_EXIT_TIMEOUT = 3.0  # 发完 Alt+Enter 等 Unity 切换显示模式的上限

# 游戏窗口类名（Win32 controller 里配置的 class_regex）
WINDOW_CLASS = "UnityWndClass"
# 游戏窗口标题子串。UnityWndClass 是所有 Unity 游戏共用的类名，
# 只按类名匹配会误抓同时在跑的其他 Unity 游戏（实测抓到过 Steam 游戏，
# resize 顶不动 4K 全屏窗口而误报"游戏锁定分辨率"；StopApp 则会误杀）。
WINDOW_TITLE = "BrownDust"

LOG_PREFIX = "[PC_ResizeWindow]"


def _log(msg: str) -> None:
    """走 print 输出（不进 maafw.log）。flush 保证 Agent 子进程的输出即时可见。

    宿主控制台若不是 UTF-8（实测 cp1252/GBK 下中文会抛 UnicodeEncodeError），
    降级成转义形式重打一次——日志本身绝不能把调用它的动作搞挂。
    """
    line = f"{LOG_PREFIX} {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "backslashreplace").decode(enc), flush=True)


def _hwnd_int(hwnd) -> int:
    """把 HWND（枚举回调里给的是 int，自己构造的是 ctypes 对象）统一成整数，仅用于打印。"""
    if isinstance(hwnd, int):
        return hwnd
    return getattr(hwnd, "value", None) or 0


def _explain_win_error(err: int) -> str:
    """把常见 Win32 错误码翻成人话。

    权限那条尤其重要——它的表象跟"窗口调不动"一模一样，最容易被误判成游戏的问题。
    """
    if err == 5:  # ERROR_ACCESS_DENIED
        return (
            "：游戏很可能以管理员权限运行而本程序不是。"
            "Windows 不允许低权限进程操作高权限进程的窗口，"
            "要么两边都用管理员启动、要么都不用"
        )
    if err == 1400:  # ERROR_INVALID_WINDOW_HANDLE
        return "：窗口句柄已失效，游戏可能刚好退出了"
    return ""


def _measure_window(user32, hwnd) -> dict:
    """测一次窗口的客户区/窗口矩形/最大化状态。调整循环每轮都要调，保持轻量。"""
    import ctypes
    import ctypes.wintypes

    win_rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(win_rect))
    cli_rect = ctypes.wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(cli_rect))
    return {
        "client_w": cli_rect.right - cli_rect.left,
        "client_h": cli_rect.bottom - cli_rect.top,
        "window_w": win_rect.right - win_rect.left,
        "window_h": win_rect.bottom - win_rect.top,
        "pos_x": win_rect.left,
        "pos_y": win_rect.top,
        "maximized": bool(user32.IsZoomed(hwnd)),
        "minimized": bool(user32.IsIconic(hwnd)),
    }


def _log_measure(stage: str, m: dict) -> None:
    """打印一次测量结果。stage 形如 '调整前' / '调整后'。"""
    state = []
    if m["maximized"]:
        state.append("最大化")
    if m["minimized"]:
        state.append("最小化")
    state_txt = f" | 状态={'+'.join(state)}" if state else ""
    _log(
        f"{stage}: 客户区 {m['client_w']}x{m['client_h']} | "
        f"窗口 {m['window_w']}x{m['window_h']} @ ({m['pos_x']},{m['pos_y']}) | "
        f"边框 {m['window_w'] - m['client_w']}x{m['window_h'] - m['client_h']}{state_txt}"
    )


# 只列与"能不能改大小 / 是不是全屏无边框"相关的位
_STYLE_BITS = [
    (0x00C00000, "WS_CAPTION"),  # 标题栏
    (0x00040000, "WS_THICKFRAME"),  # 可拖拽调整大小的边框
    (0x80000000, "WS_POPUP"),  # 无边框弹出窗，Unity 全屏用的就是它
    (0x01000000, "WS_MAXIMIZE"),
    (0x20000000, "WS_MINIMIZE"),
    (0x10000000, "WS_VISIBLE"),
]

WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000


def _describe_style(user32, hwnd) -> tuple[int, str]:
    """读窗口样式位，返回 (style, 人读的描述)。

    调不动窗口时先看这里：全屏/无边框窗口没有 WS_THICKFRAME，尺寸常被游戏自己接管。
    """
    import ctypes
    import ctypes.wintypes

    GWL_STYLE = -16
    getter = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    getter.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
    getter.restype = ctypes.c_ssize_t
    style = int(getter(hwnd, GWL_STYLE)) & 0xFFFFFFFF
    names = [name for bit, name in _STYLE_BITS if style & bit == bit]
    return style, "+".join(names) if names else "无已知样式位"


def _query_display(user32, hwnd) -> dict:
    """查窗口所在显示器的两套分辨率，用来判断系统缩放是否在捣乱。

    - virtual：`GetMonitorInfo` 报的，**受本进程 DPI 感知级别影响**——
      未感知的进程拿到的是被系统除过缩放的逻辑像素
    - physical：`EnumDisplaySettings` 报的当前显示模式，是显卡真正在输出的像素，
      跟进程感知无关

    两者不等就说明系统缩放不是 100%，而本进程还在虚拟化视角下看世界。
    """
    import ctypes
    import ctypes.wintypes

    wt = ctypes.wintypes

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("rcMonitor", wt.RECT),
            ("rcWork", wt.RECT),
            ("dwFlags", wt.DWORD),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    class DEVMODEW(ctypes.Structure):
        # 布局必须与 wingdi.h 逐字段对齐（Unicode 版共 220 字节），
        # 少一个字段 EnumDisplaySettingsW 就会写越界
        _fields_ = [
            ("dmDeviceName", ctypes.c_wchar * 32),
            ("dmSpecVersion", wt.WORD),
            ("dmDriverVersion", wt.WORD),
            ("dmSize", wt.WORD),
            ("dmDriverExtra", wt.WORD),
            ("dmFields", wt.DWORD),
            ("dmPositionX", ctypes.c_long),  # 与打印机分支共用的 union，共 16 字节
            ("dmPositionY", ctypes.c_long),
            ("dmDisplayOrientation", wt.DWORD),
            ("dmDisplayFixedOutput", wt.DWORD),
            ("dmColor", ctypes.c_short),
            ("dmDuplex", ctypes.c_short),
            ("dmYResolution", ctypes.c_short),
            ("dmTTOption", ctypes.c_short),
            ("dmCollate", ctypes.c_short),
            ("dmFormName", ctypes.c_wchar * 32),
            ("dmLogPixels", wt.WORD),
            ("dmBitsPerPel", wt.DWORD),
            ("dmPelsWidth", wt.DWORD),
            ("dmPelsHeight", wt.DWORD),
            ("dmDisplayFlags", wt.DWORD),
            ("dmDisplayFrequency", wt.DWORD),
            ("dmICMMethod", wt.DWORD),
            ("dmICMIntent", wt.DWORD),
            ("dmMediaType", wt.DWORD),
            ("dmDitherType", wt.DWORD),
            ("dmReserved1", wt.DWORD),
            ("dmReserved2", wt.DWORD),
            ("dmPanningWidth", wt.DWORD),
            ("dmPanningHeight", wt.DWORD),
        ]

    d = {
        "device": "",
        "virtual_w": 0,
        "virtual_h": 0,
        "physical_w": 0,
        "physical_h": 0,
        "refresh": 0,
        "dpi": 0,
        "scale": 0.0,
    }

    device = None
    try:
        user32.MonitorFromWindow.restype = wt.HANDLE
        user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        MONITOR_DEFAULTTONEAREST = 0x0002
        hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if hmon:
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if user32.GetMonitorInfoW(wt.HANDLE(hmon), ctypes.byref(info)):
                d["virtual_w"] = info.rcMonitor.right - info.rcMonitor.left
                d["virtual_h"] = info.rcMonitor.bottom - info.rcMonitor.top
                device = info.szDevice
                d["device"] = device
    except Exception as e:
        _log(f"取显示器信息失败（不影响调整）: {e!r}")

    if not d["virtual_w"]:
        SM_CXSCREEN, SM_CYSCREEN = 0, 1
        d["virtual_w"] = user32.GetSystemMetrics(SM_CXSCREEN)
        d["virtual_h"] = user32.GetSystemMetrics(SM_CYSCREEN)

    try:
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
        user32.EnumDisplaySettingsW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(DEVMODEW)]
        if user32.EnumDisplaySettingsW(device, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
            d["physical_w"] = int(dm.dmPelsWidth)
            d["physical_h"] = int(dm.dmPelsHeight)
            d["refresh"] = int(dm.dmDisplayFrequency)
    except Exception as e:
        _log(f"取显示模式失败（不影响调整）: {e!r}")

    if d["physical_w"] and d["virtual_w"]:
        d["scale"] = d["physical_w"] / d["virtual_w"]

    # GetDpiForWindow 需要 Win10 1607+；注意未感知的进程这里恒返回 96，
    # 所以判断缩放不能只看它，得靠上面 physical/virtual 的比值
    try:
        d["dpi"] = int(user32.GetDpiForWindow(hwnd))
    except (AttributeError, OSError, ValueError):
        d["dpi"] = 0

    return d


def _is_scaled(display: dict) -> bool:
    """系统缩放是否不等于 100%（比值法，不依赖本进程的 DPI 感知级别）。"""
    return bool(display["scale"]) and abs(display["scale"] - 1.0) > 0.001


def _log_display(display: dict) -> None:
    """打印用户当前的显示环境：物理分辨率、本进程看到的分辨率、DPI。"""
    dev = f" [{display['device']}]" if display["device"] else ""
    phys = f"{display['physical_w']}x{display['physical_h']}" if display["physical_w"] else "未知"
    refresh = f" @{display['refresh']}Hz" if display["refresh"] else ""
    _log(f"显示器{dev}: 物理分辨率 {phys}{refresh}")
    _log(
        f"本进程看到的分辨率: {display['virtual_w']}x{display['virtual_h']} | "
        f"GetDpiForWindow={display['dpi'] or '未知'}"
    )
    if _is_scaled(display):
        _log(
            f"⚠️ 两者不一致，系统缩放约 {round(display['scale'] * 100)}%——"
            f"本进程处于 DPI 虚拟化视角，按这个读数设窗口必然差几个像素"
        )


_DPI_AWARENESS_RESULT = None


def _ensure_dpi_awareness() -> str:
    """把本进程提升到 Per-Monitor-V2 DPI 感知，返回一句话结果（进程内只做一次）。

    不提升的话，系统缩放不是 100% 时 GetClientRect / SetWindowPos 走的都是被系统
    除过缩放的逻辑像素。想要客户区物理 1280x720，在 150% 缩放下得设 853.33 逻辑像素
    ——这个值根本没法表达，于是无论怎么补偿都差 1~2 px。这正是"偏差不大但就是调不准"
    的根因，只有让本进程按物理像素说话才能真正对齐。

    Agent 是独立于 MaaFramework 主进程的 Python 进程（靠 socket_id 连回去），
    改它的 DPI 感知只影响本进程读到的窗口坐标，不动主进程的截图与点击。
    本文件之外，agent/ 里其余 ctypes 调用都只碰 kernel32 的进程/句柄，不涉及窗口坐标。
    """
    global _DPI_AWARENESS_RESULT
    if _DPI_AWARENESS_RESULT is not None:
        return _DPI_AWARENESS_RESULT

    import ctypes

    user32 = ctypes.windll.user32

    # Win10 1703+：Per-Monitor-V2，跨屏拖动也跟着变，最准
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _DPI_AWARENESS_RESULT = "已提升为 Per-Monitor-V2"
            return _DPI_AWARENESS_RESULT
        first = f"SetProcessDpiAwarenessContext 失败(err={ctypes.GetLastError()})"
    except (AttributeError, OSError) as e:
        first = f"SetProcessDpiAwarenessContext 不可用({e.__class__.__name__})"

    # Win8.1+ 回退
    try:
        hr = ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        if hr == 0:
            _DPI_AWARENESS_RESULT = f"已提升为 Per-Monitor（{first}，转 shcore 回退）"
            return _DPI_AWARENESS_RESULT
        second = f"SetProcessDpiAwareness 返回 hr=0x{hr & 0xFFFFFFFF:08X}"
    except (AttributeError, OSError) as e:
        second = f"shcore 不可用({e.__class__.__name__})"

    # Vista+ 最后回退：只按主屏 DPI 感知
    try:
        if user32.SetProcessDPIAware():
            _DPI_AWARENESS_RESULT = f"已提升为 System-Aware（{first}；{second}）"
            return _DPI_AWARENESS_RESULT
    except (AttributeError, OSError):
        pass

    # 三条都没成通常意味着感知级别已被 manifest 钉死，究竟钉在哪一档看不出来，
    # 只能靠调用方对比提升前后的读数来判断
    _DPI_AWARENESS_RESULT = f"未能提升（{first}；{second}）"
    return _DPI_AWARENESS_RESULT


def _looks_fullscreen(style: int, m: dict, display: dict) -> bool:
    """判断窗口是不是处于（无边框）全屏。

    两条同时成立才算：既没有标题栏也没有可调边框，且尺寸铺满了整块显示器。
    只看样式会把无边框皮肤的窗口误判，只看尺寸会把最大化误判。
    """
    if style & WS_THICKFRAME or style & WS_CAPTION:
        return False
    mon_w = display["physical_w"] or display["virtual_w"]
    mon_h = display["physical_h"] or display["virtual_h"]
    if not mon_w or not mon_h:
        return False
    return m["window_w"] >= mon_w and m["window_h"] >= mon_h


VK_RETURN = 0x0D
VK_MENU = 0x12  # Alt
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105


def _post_alt_enter(user32, hwnd) -> bool:
    """用 PostMessage 给窗口投一次 Alt+Enter，不抢焦点。

    lParam 的第 29 位是 context code，置 1 才表示"Alt 同时按着"——Unity 的窗口过程
    认的就是这个位，漏了它这几条消息会被当成普通回车。
    扫描码：Alt=0x38、Enter=0x1C。
    """
    return all(
        user32.PostMessageW(hwnd, msg, vk, lparam)
        for msg, vk, lparam in (
            (WM_SYSKEYDOWN, VK_MENU, 0x20380001),
            (WM_SYSKEYDOWN, VK_RETURN, 0x201C0001),
            (WM_SYSKEYUP, VK_RETURN, 0xE01C0001),
            (WM_SYSKEYUP, VK_MENU, 0xE0380001),
        )
    )


def _synth_alt_enter(user32, hwnd) -> bool:
    """抢到前台后合成真实的 Alt+Enter 按键。

    PostMessage 不奏效时的退路——部分 Unity 版本走 Raw Input，只认真实输入队列里的键。
    代价是会把游戏窗口抢到前台；抢不到就不发，免得 Alt+Enter 打到别的窗口上去。
    """
    import ctypes

    user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    if user32.GetForegroundWindow() != _hwnd_int(hwnd):
        _log("没能把游戏窗口抢到前台，跳过真实按键（避免 Alt+Enter 打到别的窗口）")
        return False

    KEYEVENTF_KEYUP = 0x0002
    for vk, scan, flags in (
        (VK_MENU, 0x38, 0),
        (VK_RETURN, 0x1C, 0),
        (VK_RETURN, 0x1C, KEYEVENTF_KEYUP),
        (VK_MENU, 0x38, KEYEVENTF_KEYUP),
    ):
        user32.keybd_event(vk, scan, flags, 0)
        time.sleep(0.05)
    return True


def _exit_fullscreen(user32, hwnd, display: dict) -> bool:
    """把全屏的游戏窗口切回窗口化，成功返回 True。

    走 Alt+Enter，让游戏自己切——这是 Unity 播放器内建的全屏切换键，由它自己改窗口
    样式和渲染模式。比我们从外面硬去掉 WS_POPUP 稳得多：硬改样式多半会被游戏立刻改
    回来，还容易让渲染尺寸和窗口对不上。全屏时 Unity 会直接拒掉 SetWindowPos，
    所以这一步不做，后面的调整循环怎么补偿都没用。
    """
    for label, sender in (("PostMessage", _post_alt_enter), ("真实按键", _synth_alt_enter)):
        _log(f"用 {label} 发 Alt+Enter 尝试退出全屏")
        if not sender(user32, hwnd):
            continue
        # Unity 切换显示模式要时间，轮询到不再是全屏为止
        deadline = time.time() + FULLSCREEN_EXIT_TIMEOUT
        while time.time() < deadline:
            time.sleep(0.2)
            style, style_txt = _describe_style(user32, hwnd)
            m = _measure_window(user32, hwnd)
            if not _looks_fullscreen(style, m, display):
                _log(f"已退出全屏: 样式 0x{style:08X} = {style_txt}")
                return True
        _log(f"{label} 没奏效（等了 {FULLSCREEN_EXIT_TIMEOUT}s 仍是全屏）")
    return False


def _find_game_hwnd():
    """枚举顶层窗口，返回首个 类名+标题 都匹配且可见的游戏窗口 hwnd（找不到返回 HWND(0)，falsy）。

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
            title_buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title_buf, 256)
            if WINDOW_TITLE in title_buf.value:
                found = hwnd
                return False  # 停止枚举
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
    return found


def _hit_target(m: dict) -> bool:
    """客户区是否正好是目标尺寸。"""
    return m["client_w"] == TARGET_WIDTH and m["client_h"] == TARGET_HEIGHT


def _resize_to_target(user32, hwnd) -> tuple[bool, dict]:
    """把客户区收敛到 TARGET_WIDTH x TARGET_HEIGHT，返回 (是否达标, 最后一次测量)。

    刻意不做"一次算准边框"：DPI 取整、DWM 那圈不可见的调整边框、以及 Unity 自己
    回拉窗口，任何一条都能让一次性推算差上几像素。这里改成闭环——测出客户区与目标
    的差值，原样加到窗口总尺寸上，再测再补。边框到底多厚、缩放怎么取整都不必知道，
    通常 1~2 轮到位。
    """
    import ctypes

    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010  # 别把游戏窗口抢到前台
    SWP_FRAMECHANGED = 0x0020
    flags = SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED

    touched = False
    m = _measure_window(user32, hwnd)

    for attempt in range(1, MAX_RESIZE_ATTEMPTS + 1):
        if _hit_target(m):
            if not touched:
                return True, m
            # 是我们改到位的，等一下再确认：Unity 有时会把窗口尺寸改回它自己那套
            time.sleep(STABILIZE_DELAY)
            m = _measure_window(user32, hwnd)
            if _hit_target(m):
                return True, m
            _log(
                f"第{attempt}轮: 达标后又被改回 {m['client_w']}x{m['client_h']}"
                f"（游戏自己重设了窗口），继续修"
            )

        dw = TARGET_WIDTH - m["client_w"]
        dh = TARGET_HEIGHT - m["client_h"]
        new_w = m["window_w"] + dw
        new_h = m["window_h"] + dh
        _log(
            f"第{attempt}轮: 客户区 {m['client_w']}x{m['client_h']} 差 {dw:+d}x{dh:+d} → "
            f"窗口 {m['window_w']}x{m['window_h']} 设为 {new_w}x{new_h}"
        )

        if not user32.SetWindowPos(hwnd, 0, 0, 0, new_w, new_h, flags):
            err = ctypes.GetLastError()
            _log(f"SetWindowPos 失败(err={err}){_explain_win_error(err)}")
            return False, m

        touched = True
        time.sleep(SETTLE_DELAY)
        prev = m
        m = _measure_window(user32, hwnd)

        # SetWindowPos 报成功但尺寸一动没动：窗口在拒绝这个大小（最小尺寸限制、
        # 或者尺寸被游戏自己接管）。再补几轮只会刷出同样的日志，就此打住。
        if (m["client_w"], m["client_h"], m["window_w"], m["window_h"]) == (
            prev["client_w"],
            prev["client_h"],
            prev["window_w"],
            prev["window_h"],
        ):
            _log("窗口尺寸纹丝未动——调用成功但被窗口拒绝，不再重试")
            return False, m

    if _hit_target(m):
        return True, m
    _log(f"已试 {MAX_RESIZE_ATTEMPTS} 轮仍未收敛，放弃")
    return False, m


def _log_failure_hints(m: dict, style: int, display: dict) -> None:
    """调不到位时给出可查的方向。

    ⚠️ 不要再说"游戏锁定了分辨率"——游戏内的分辨率设置只决定渲染，窗口大小是可以
    随便拉的，两者互不相干。调不动一定另有原因，下面几条才是真候选。
    """
    _log("排查方向：")
    if _looks_fullscreen(style, m, display):
        _log("  · 窗口仍是全屏，自动 Alt+Enter 没生效——请在游戏里按 Alt+Enter 切回窗口模式")
    elif not style & WS_THICKFRAME:
        _log("  · 窗口没有 WS_THICKFRAME（可调边框），尺寸被游戏接管了")
    elif not style & WS_CAPTION:
        _log("  · 窗口没有标题栏，疑似无边框模式")
    if m["client_w"] > TARGET_WIDTH or m["client_h"] > TARGET_HEIGHT:
        _log("  · 窗口比目标还大且拉不下来，可能设了最小尺寸限制")
    if _is_scaled(display):
        _log("  · 系统缩放不是 100%，若上面的 DPI 感知提升没成功，就无法按物理像素对齐")
    _log("  · 游戏若以管理员权限运行而本程序不是，SetWindowPos 会被系统拦下")
    _log("  · 游戏内的分辨率设置只影响渲染、与窗口大小无关，不需要去改它")


def _short_advice(m: dict, style: int, display: dict) -> str:
    """挑一条最可能的原因，跟着结果一起报给用户（GUI 那条只有一行，塞不下全部）。"""
    if _looks_fullscreen(style, m, display):
        return "，请在游戏里按 Alt+Enter 切回窗口模式"
    if m["client_w"] > TARGET_WIDTH or m["client_h"] > TARGET_HEIGHT:
        return "，窗口拉不到这么小，可能有最小尺寸限制"
    if _is_scaled(display):
        return f"，系统缩放 {round(display['scale'] * 100)}% 可能影响对齐"
    return "，识别可能受影响"


def _find_and_resize_window() -> tuple[bool, str]:
    """
    查找游戏窗口并把客户区调整到 1280x720。

    Returns:
        (success: bool, message: str)

    注意：调用方 `PC_ResizeWindow.run` 无论这里返回什么都向框架报成功，原因见那里的
    注释。这个返回值只决定打印哪一档信息。
    """
    if sys.platform != "win32":
        _log(f"平台={sys.platform}，非 Windows，跳过窗口调整")
        return True, "非Windows平台，跳过窗口调整"

    try:
        import ctypes

        user32 = ctypes.windll.user32

        _log(f"查找游戏窗口：类名='{WINDOW_CLASS}' 且标题含'{WINDOW_TITLE}'")
        hwnd = _find_game_hwnd()
        if not hwnd:
            _log("未找到游戏窗口（游戏未启动，或窗口不可见）")
            _log("PC 端没有自动启动游戏的能力（那条链走 ADB StartApp，PC 包里已禁用），请先手动启动游戏")
            return False, f"未找到 '{WINDOW_CLASS}'+标题含'{WINDOW_TITLE}' 的游戏窗口，请先手动启动游戏"

        # 获取窗口标题用于日志
        title_buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title_buf, 256)
        title = title_buf.value
        _log(f"命中窗口: '{title}' (hwnd=0x{_hwnd_int(hwnd):X})")

        # ---- 调整前：把用户当前的显示环境与窗口读数打全 ----
        display = _query_display(user32, hwnd)
        _log_display(display)
        style, style_txt = _describe_style(user32, hwnd)
        _log(f"窗口样式: 0x{style:08X} = {style_txt}")
        before = _measure_window(user32, hwnd)
        _log_measure("调整前", before)

        # 有系统缩放时上面这些读数全是逻辑像素，先把 DPI 感知提上来再重测一遍。
        # 缩放正好 100% 时不动进程状态——那种情况本来就没有虚拟化，改了只有风险没有收益。
        if _is_scaled(display):
            _log(f"DPI 感知: {_ensure_dpi_awareness()}")
            display = _query_display(user32, hwnd)
            _log_display(display)
            before = _measure_window(user32, hwnd)
            _log_measure("调整前(DPI 感知提升后)", before)

        # 全屏时 Unity 会直接拒掉 SetWindowPos（实测调用成功但尺寸纹丝不动），
        # 必须先让游戏自己切回窗口化，后面的补偿循环才有意义
        exited_fullscreen = False
        if _looks_fullscreen(style, before, display):
            _log("窗口处于全屏，先切回窗口化再调整")
            if _exit_fullscreen(user32, hwnd, display):
                exited_fullscreen = True
                style, style_txt = _describe_style(user32, hwnd)
                _log(f"窗口样式: 0x{style:08X} = {style_txt}")
                before = _measure_window(user32, hwnd)
                _log_measure("退出全屏后", before)
            else:
                _log("未能自动退出全屏，接下来的调整多半会被游戏拒绝")

        # 最大化/最小化状态下的窗口矩形推不出正常边框，先还原再进调整循环
        if before["maximized"] or before["minimized"]:
            _log("窗口处于最大化/最小化，先 SW_RESTORE 还原")
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(SETTLE_DELAY)
            before = _measure_window(user32, hwnd)
            _log_measure("还原后", before)

        ok, after = _resize_to_target(user32, hwnd)
        _log_measure("调整后", after)

        if ok:
            # 注意 before 是退出全屏之后重测的，所以"尺寸没变"不等于"什么都没做"——
            # 切出全屏本身就是个用户该知道的状态改变，报告里得说出来
            resized = (before["client_w"], before["client_h"]) != (
                after["client_w"],
                after["client_h"],
            )
            if resized:
                _log(
                    f"调整成功: {before['client_w']}x{before['client_h']} → "
                    f"{after['client_w']}x{after['client_h']}"
                )
            else:
                _log(f"客户区已是 {TARGET_WIDTH}x{TARGET_HEIGHT}，尺寸无需再动")

            if exited_fullscreen:
                return True, (
                    f"窗口 '{title}' 已从全屏切回窗口化，{TARGET_WIDTH}x{TARGET_HEIGHT} 就绪"
                )
            if resized:
                return True, f"窗口 '{title}' 已调整为 {TARGET_WIDTH}x{TARGET_HEIGHT}"
            return True, f"窗口 '{title}' 已是 {TARGET_WIDTH}x{TARGET_HEIGHT}，无需调整"

        _log(
            f"调整未达标: {before['client_w']}x{before['client_h']} → "
            f"{after['client_w']}x{after['client_h']}（期望 {TARGET_WIDTH}x{TARGET_HEIGHT}）"
        )
        _log_failure_hints(after, style, display)
        return False, (
            f"窗口停在 {after['client_w']}x{after['client_h']}，未能调到 "
            f"{TARGET_WIDTH}x{TARGET_HEIGHT}{_short_advice(after, style, display)}"
        )

    except ImportError as e:
        _log(f"缺少依赖: {e}")
        return False, f"缺少依赖: {e}"
    except Exception as e:
        import traceback

        _log(f"调整窗口时发生异常: {e!r}")
        _log(f"traceback:\n{traceback.format_exc()}")
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

    **恒定报成功**，调整结果只体现在打印里，不影响流程走向。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        # 给用户看的只有开始/结果这两条，详细过程一律走 print 留在控制台日志里
        mfaalog.info(f"[PC窗口] 🖥️ 开始检查游戏窗口 (目标 {TARGET_WIDTH}x{TARGET_HEIGHT})")

        success, message = _find_and_resize_window()

        if success:
            _log(f"✅ {message}")
            mfaalog.info(f"[PC窗口] ✅ {message}")
        else:
            _log(f"⚠️ {message}")
            _log("窗口未就绪，但本动作仍报成功——交给 next 的识别节点判断当前在哪一步")
            mfaalog.warning(f"[PC窗口] ⚠️ {message}")

        # 恒定返回 True：这是个"尽力而为"的动作，不是流程闸门。
        # 返回 False 会让节点进入异常态，next 整条都不再走；而 PC 覆盖包里
        # StartGame_RunApp 与 StartGame_Check_App_Alive 都因依赖 ADB 被 enabled:false
        # 关掉了，on_error 接不住任何东西，结果就是整个 StartGame task 停在这里，
        # 游戏再也起不来。窗口没调好顶多识别不准，不该把启动流程整个掐断。
        return True


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
            return True, f"未找到 '{WINDOW_CLASS}'+标题含'{WINDOW_TITLE}' 游戏窗口，游戏可能已关闭，跳过"

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
