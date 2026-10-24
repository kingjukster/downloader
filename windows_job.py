"""Windows process-tree lifetime guard. No shell, services or PID-based kills."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time


def kernel():
    if os.name != 'nt':
        raise OSError('Download execution requires Windows Job Objects; no uncontained fallback')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        'GetCurrentProcess': ([], wintypes.HANDLE),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        method = getattr(api, name)
        method.argtypes, method.restype = args, result
    return api


class BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                ('max_working_set', ctypes.c_size_t), ('active_process_limit', wintypes.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', BasicLimits), ('io', ctypes.c_uint64 * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]


class Accounting(ctypes.Structure):
    _fields_ = [('times', ctypes.c_int64 * 4), ('faults', wintypes.DWORD),
                ('total', wintypes.DWORD), ('active', wintypes.DWORD), ('terminated', wintypes.DWORD)]


def checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


class WindowsJob:
    def __enter__(self):
        self.api = kernel()
        self.handle = checked(self.api.CreateJobObjectW(None, None))
        try:
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
            checked(self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.api.CloseHandle(self.handle)
            raise
        return self

    def start(self, argv, log):
        # Helper joins the inherited job before executing any downloader code.
        # It then closes its copy so controller death closes the last job handle.
        os.set_handle_inheritable(self.handle, True)
        info = subprocess.STARTUPINFO()
        info.lpAttributeList = {'handle_list': [self.handle]}
        ready = Path(log.name).with_name('scope-ready')
        if ready.exists():
            raise OSError('Unexpected scope readiness evidence; refusing reuse')
        try:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(self.handle), str(ready), *argv[1:]],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    startupinfo=info, creationflags=subprocess.CREATE_NO_WINDOW,
                                    close_fds=True)
        finally:
            os.set_handle_inheritable(self.handle, False)
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None:
                raise OSError('Download helper exited before confirming containment; inspect private worker.log')
            if time.monotonic() >= deadline:
                raise OSError('Download helper did not confirm containment; outcome requires inspection')
            time.sleep(0.02)
        return process

    def stop(self):
        checked(self.api.TerminateJobObject(self.handle, 1))
        deadline = time.monotonic() + 10
        while True:
            accounting = Accounting()
            checked(self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
            if accounting.active == 0:
                return
            if time.monotonic() >= deadline:
                raise OSError('Download process-tree closure could not be verified')
            time.sleep(0.05)

    def __exit__(self, *unused):
        self.api.CloseHandle(self.handle)


if __name__ == '__main__':
    api = kernel()
    inherited = int(sys.argv[1])
    checked(api.AssignProcessToJobObject(inherited, api.GetCurrentProcess()))
    checked(api.CloseHandle(inherited))
    Path(sys.argv[2]).write_text('contained', encoding='ascii')
    sys.argv = sys.argv[3:]
    runpy.run_path(sys.argv[0], run_name='__main__')
