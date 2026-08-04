# -*- coding: utf-8 -*-
"""宿主 (UI) 存活守护。

【为什么需要它】
MaaFramework 的 AgentServer 侧接收超时恒为 `milliseconds::max()`
(source/include/MaaAgent/Transceiver.h:118)，`Transceiver::poll()` 里
`if (elapsed > timeout_)` 因此永远为假，`recv()` 会以 1 秒为粒度永远轮询下去。
退出 `request_msg_loop` 只有一条路：UI 侧发来 ShutDownRequest。而 UI 被强杀、
崩溃、或调试会话被拔掉时，`AgentClient::disconnect()` 根本不执行，那条消息永远
不会来 —— `AgentServer.join()` 于是永不返回，Agent 进程永久驻留，仍持有 socket。

MaaAgentServerAPI.h 只暴露 StartUp / ShutDown / Join / Detach，**没有任何 timeout
设置接口**（`set_timeout` 仅 AgentClient 侧调用），所以这一层只能由我们自己补。

【实现要点】
Windows 上在启动时就 `OpenProcess(SYNCHRONIZE)` 抓住父进程句柄，之后一直用这个
句柄 `WaitForSingleObject`：
  · 句柄绑定的是那个具体的内核对象，**PID 被复用也不会误判**；
  · 父进程一退出，等待立即返回，不必等到下一个轮询周期。
POSIX 上退化为 `getppid()` 变化检测（父进程死后子进程会被 reparent）。

拿不到句柄时 `available` 为 False，调用方应回退到原来的阻塞 join，
行为不会比现状更差。
"""

import os
import sys
import time

from . import mfaalog

# --- Windows API 常量 ---
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
# 其余返回值(WAIT_FAILED=0xFFFFFFFF、WAIT_ABANDONED 等)一律按「句柄失效 → 宿主已没」处理


class HostWatchdog:
    """监视父进程（即启动本 Agent 的 UI 进程）是否退出。"""

    def __init__(self) -> None:
        self.ppid = 0
        self.available = False
        self._handle = None
        self._kernel32 = None

        try:
            self.ppid = os.getppid()
        except OSError as e:
            mfaalog.warning(f"[Watchdog] 无法获取父进程 PID: {e}")
            return

        if self.ppid <= 0:
            mfaalog.warning(f"[Watchdog] 父进程 PID 非法 ({self.ppid})，守护不可用")
            return

        if sys.platform == "win32":
            self._init_windows()
        else:
            # POSIX 下 getppid() 变化即可判定，无需额外资源
            self.available = True

    # ------------------------------------------------------------------
    # Windows
    # ------------------------------------------------------------------
    def _init_windows(self) -> None:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.WaitForSingleObject.restype = ctypes.c_ulong
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

            # SYNCHRONIZE 是能 Wait 所需的最小权限，比 PROCESS_QUERY_INFORMATION 更容易拿到
            handle = kernel32.OpenProcess(_SYNCHRONIZE, False, self.ppid)
            if not handle:
                err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
                mfaalog.warning(
                    f"[Watchdog] OpenProcess 失败 (ppid={self.ppid}, err={err})，"
                    "将回退为阻塞等待，UI 异常退出时本进程可能残留"
                )
                return

            self._kernel32 = kernel32
            self._handle = handle
            self.available = True
        except Exception as e:
            mfaalog.warning(f"[Watchdog] Windows 守护初始化失败: {e}")

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def host_exited(self, timeout: float) -> bool:
        """阻塞至多 timeout 秒等待父进程退出。

        Returns
        -------
        bool
            True  = 父进程已退出（UI 没了）
            False = 超时，父进程仍在
        """
        if not self.available:
            time.sleep(timeout)
            return False

        if self._handle is not None and self._kernel32 is not None:
            ret = self._kernel32.WaitForSingleObject(self._handle, int(timeout * 1000))
            if ret == _WAIT_OBJECT_0:
                return True
            if ret == _WAIT_TIMEOUT:
                return False
            # WAIT_FAILED 等异常返回：句柄已失效，按父进程已消失处理（保守方向是退出）
            mfaalog.warning(f"[Watchdog] WaitForSingleObject 返回异常值 {ret:#x}，按宿主已退出处理")
            return True

        # POSIX: 轮询 getppid()。父进程退出后子进程被 reparent，ppid 必然改变。
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.getppid() != self.ppid:
                    return True
            except OSError:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.5, remaining))

    def close(self) -> None:
        if self._handle is not None and self._kernel32 is not None:
            try:
                self._kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None


def cleanup_socket_file(socket_id: str) -> None:
    """删除 IPC 模式下残留的 socket 文件。

    正常退出时由 Transceiver 的析构负责删除，但宿主失联后我们只能 os._exit()
    （见 main.py 的说明），析构不会跑，所以在这里补一刀。
    纯数字的 identifier 是 TCP 模式，没有文件。
    """
    if not socket_id or socket_id.isdigit():
        return
    try:
        from pathlib import Path

        # 路径与 AgentCommon/Transceiver.cpp 的 temp_directory() 保持一致
        base = Path("C:/Temp") if sys.platform == "win32" else Path("/tmp")
        sock = base / f"maafw-agent-{socket_id}.sock"
        if sock.exists():
            sock.unlink()
            mfaalog.debug(f"[Watchdog] 已清理残留 socket: {sock}")
    except Exception as e:
        mfaalog.debug(f"[Watchdog] 清理 socket 文件失败（不影响退出）: {e}")
