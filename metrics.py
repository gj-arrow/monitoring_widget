import psutil
import os
import logging

def cpu_pct() -> int:
    return psutil.cpu_percent(interval=0.1)

def ram_used_gb() -> float:
    v = psutil.virtual_memory()
    return round(v.used / 1024**3, 1)


def ram_total_gb() -> float:
    v = psutil.virtual_memory()
    return round(v.total / 1024**3, 1)

def gpu_utilization() -> str:
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        return str(util.gpu)
    except Exception:
        pass

    try:
        import wmi
        c = wmi.WMI()
        gpu_objs = c.Win32_VideoController()
        if gpu_objs:
            base_clk = getattr(gpu_objs[0], "CurrentClockFrequency", 0) or 0
            max_clk = getattr(gpu_objs[0], "MaxClockSpeed", 0) or 0
            if max_clk:
                ratio = min(base_clk / max_clk, 1.0)
                est = int(ratio * 30)
                return str(est)
    except Exception:
        pass

    return "0"

def gpu_vram_used_gb() -> float:
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        return round(mem.used / 1024**3, 1)
    except Exception:
        pass

    try:
        import wmi
        c = wmi.WMI()
        vcs = c.Win32_VideoController()
        if vcs:
            dmem = int(getattr(vcs[0], "AdapterRAM", 0) or 0)
            total_gb = round(dmem / 1024**3, 1) if dmem else 0
            return round(total_gb * 0.15, 1)
    except Exception:
        pass

    return 0.0

def gpu_vram_total_gb() -> float:
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        return round(mem.total / 1024 ** 3, 1)
    except Exception:
        pass

    try:
        import wmi
        c = wmi.WMI()
        vcs = c.Win32_VideoController()
        if vcs:
            dmem = int(getattr(vcs[0], "AdapterRAM", 0) or 0)
            return round(dmem / 1024 ** 3, 1) if dmem else 0
    except Exception:
        pass

    return 0.0

def gpu_temp() -> float:
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        return float(temp)
    except Exception:
        pass

    try:
        import wmi
        c = wmi.WMI()
        gpu_objs = c.Win32_VideoController()
        if gpu_objs:
            # WMI не всегда предоставляет температуру напрямую без специальных драйверов, 
            # оставляем заглушку или пробуем альтернативный путь, если он известен
            pass
    except Exception:
        pass

    return 0.0
