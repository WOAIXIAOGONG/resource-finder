"""Check only process IDs explicitly registered by the cooperative task runner.

No process enumeration, command-line inspection or Agent-app discovery. A runner
and its child must both retain their creation identity; reused PIDs do not count.
"""
import os
import threading
import secrets


def process_identity(pid):
    try:
        pid = int(pid)
        if pid <= 0:return None
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = kernel.OpenProcess(0x101000, False, pid)
            if not handle:return None
            try:
                code = wintypes.DWORD()
                if kernel.WaitForSingleObject(handle,0)!=258:return None
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:return None
                values = [wintypes.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):return None
                return (pid, values[0].dwHighDateTime << 32 | values[0].dwLowDateTime)
            finally:kernel.CloseHandle(handle)
        from pathlib import Path
        fields = Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else (pid, fields[19])
    except (OSError, ValueError, TypeError, IndexError):return None


class RunnerTracker:
    def __init__(self):
        self.rows = {}; self.lock = threading.RLock()

    def register(self, task_id, runner_pid, child_pid, runner_created=None, child_created=None):
        runner, child = process_identity(runner_pid), process_identity(child_pid)
        if not runner or not child or runner == child or runner[0] == os.getpid() or child[0] == os.getpid():
            raise ValueError('运行助手及其搜索子程序必须仍在运行')
        if (runner_created is not None and str(runner_created)!=str(runner[1]) or
                child_created is not None and str(child_created)!=str(child[1])):
            raise ValueError('进程创建标识不匹配，无法跨进程命名空间检测；请使用控制心跳')
        key = secrets.token_hex(12)
        with self.lock:
            self.rows[key] = dict(task_id=task_id, runner=runner, child=child)
        return key

    def remove(self, key, task_id):
        with self.lock:
            if self.rows.get(key, {}).get('task_id') == task_id:self.rows.pop(key, None)

    def active(self, task_id):
        with self.lock:
            alive = 0
            for key, row in list(self.rows.items()):
                if (row['task_id'] != task_id or process_identity(row['runner'][0]) != row['runner'] or
                        process_identity(row['child'][0]) != row['child']):
                    self.rows.pop(key, None)
                else:alive += 1
            return alive

    def clear(self):
        with self.lock:self.rows.clear()
