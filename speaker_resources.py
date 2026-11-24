"""Memory budgets for the isolated Windows speaker worker (not the web server)."""
import ctypes
from ctypes import wintypes
import os

GIB = 1024 ** 3
_job_handle = None


class BasicLimits(ctypes.Structure):
    _fields_ = [('ProcessTime', ctypes.c_longlong), ('JobTime', ctypes.c_longlong),
                ('Flags', wintypes.DWORD), ('MinWorkingSet', ctypes.c_size_t),
                ('MaxWorkingSet', ctypes.c_size_t), ('ActiveProcesses', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('Priority', wintypes.DWORD), ('Scheduling', wintypes.DWORD)]


class IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('Basic', BasicLimits), ('IO', IOCounters), ('ProcessMemory', ctypes.c_size_t),
                ('JobMemory', ctypes.c_size_t), ('PeakProcessMemory', ctypes.c_size_t), ('PeakJobMemory', ctypes.c_size_t)]


class MemoryCounters(ctypes.Structure):
    _fields_ = [('Size', wintypes.DWORD), ('PageFaults', wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in ('PeakWorkingSet', 'WorkingSet', 'PeakPagedPool', 'PagedPool',
                                          'PeakNonPagedPool', 'NonPagedPool', 'Pagefile', 'PeakPagefile', 'Private')]


def limit_ram(gib=4):
    """Cap resident working set and private commit; address space remains unrestricted for CUDA."""
    global _job_handle
    if not 0 < gib <= 4:
        raise ValueError('Лимит RAM должен быть больше 0 и не выше 4 ГиБ.')
    if os.name != 'nt':
        raise RuntimeError('Ограничение RAM этого приложения поддерживается в Windows.')
    budget = int(gib * GIB)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.SetProcessWorkingSetSizeEx.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel.GetCurrentProcess()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.Basic.Flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY (private committed memory)
        limits.ProcessMemory = budget
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.SetProcessWorkingSetSizeEx(process, 8 * 1024 ** 2, budget, 0x4):  # hard working-set maximum
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(job)
        raise
    _job_handle = job


def memory_usage():
    if os.name != 'nt':
        return {}
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    counters = MemoryCounters()
    counters.Size = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.Size):
        raise ctypes.WinError(ctypes.get_last_error())
    return {'ram_peak_bytes': counters.PeakWorkingSet, 'ram_private_bytes': counters.Private}


def configure_gpu(torch, vram_gib=4):
    if not 0 < vram_gib <= 4:
        raise ValueError('Лимит VRAM должен быть больше 0 и не выше 4 ГиБ.')
    total = torch.cuda.get_device_properties(0).total_memory
    free, _ = torch.cuda.mem_get_info(0)
    # CUDA context/libraries allocate outside PyTorch's allocator. Leave them room.
    budget = min(vram_gib * GIB - 256 * 1024 ** 2, free - 256 * 1024 ** 2, total - 256 * 1024 ** 2)
    if budget < 256 * 1024 ** 2:
        raise torch.cuda.OutOfMemoryError('Недостаточно свободной видеопамяти.')
    torch.cuda.set_per_process_memory_fraction(budget / total, device=0)
    torch.backends.cudnn.benchmark = False
    return int(budget)
