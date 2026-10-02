"""Reading hardware counters.

One Snapshot per tick. Every field is float | None, where None means "not
measured" and the renderer draws a dash. Nothing in here invents a number:
an estimate that looks like a measurement is worse than no reading at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("widget.metrics")

_GB = 1024 ** 3

GPU_KEYS = ("gpu_pct", "gpu_temp_c", "vram_used_gb", "vram_total_gb")


@dataclass(frozen=True, slots=True)
class Snapshot:
    cpu_pct: float | None = None
    cpu_mhz: float | None = None
    cpu_max_mhz: float | None = None
    ram_used_gb: float | None = None
    ram_total_gb: float | None = None
    gpu_pct: float | None = None
    gpu_temp_c: float | None = None
    vram_used_gb: float | None = None
    vram_total_gb: float | None = None
    ts: float = 0.0

    # asdict() follows declaration order, which would put ts last. The CSV
    # writer and this list must agree or the trace is silently column-shifted,
    # so the order is stated once here and the header is derived from it.
    CSV_COLUMNS = (
        "ts", "cpu_pct", "cpu_mhz", "cpu_max_mhz",
        "ram_used_gb", "ram_total_gb",
        "gpu_pct", "gpu_temp_c", "vram_used_gb", "vram_total_gb",
    )

    def as_dict(self) -> dict[str, float | None]:
        return {name: getattr(self, name) for name in self.CSV_COLUMNS}


def _empty_gpu() -> dict[str, float | None]:
    return dict.fromkeys(GPU_KEYS)


def _wmi_video_controllers() -> list[Any]:
    """Win32_VideoController instances, or [] when WMI is unusable."""
    try:
        import wmi

        return list(wmi.WMI().Win32_VideoController())
    except Exception:
        logger.info("WMI unavailable")
        return []


def wmi_fallback() -> dict[str, float | None]:
    """Only what WMI can actually measure, and nothing more.

    The old code estimated GPU load as clock_ratio * 30. That is a made-up
    percentage wearing the costume of a measurement, so it is gone: load and
    temperature come back as None and the panel shows a dash.
    """
    out = _empty_gpu()
    controllers = _wmi_video_controllers()
    if not controllers:
        return out
    # AdapterRAM is a signed int32, so every card with 2 GB or more of VRAM
    # overflows it and arrives negative -- the 12 GB card on this machine
    # reports -1048576, which is 0xFF000000. Only a strictly positive value is
    # a size; anything else was never measured, and dividing it anyway
    # produced -0.0 here and -1.0 for an 8 GB card.
    adapter_ram = getattr(controllers[0], "AdapterRAM", 0) or 0
    if adapter_ram > 0:
        out["vram_total_gb"] = round(float(adapter_ram) / _GB, 1)
    return out


class GpuProbe:
    """Holds one NVML handle for the life of the process.

    The previous code called nvmlInit() once per metric, so a tick cost four
    initialisations, and a machine without NVIDIA raised eight exceptions
    every two seconds instead of answering once at startup.
    """

    def __init__(self, nvml: Any | None = None) -> None:
        if nvml is None:
            try:
                import pynvml as nvml  # type: ignore[no-redef]
            except Exception:
                logger.info("pynvml not importable, GPU metrics disabled")
                nvml = None
        self._nvml = nvml
        self._handle: Any | None = None
        if nvml is not None:
            try:
                nvml.nvmlInit()
                self._handle = nvml.nvmlDeviceGetHandleByIndex(0)
            except Exception:
                logger.info("NVML present but no usable device, falling back to WMI")
                self._handle = None
            else:
                logger.info("NVML ready: %s", self._device_name())

    def _device_name(self) -> str:
        """The device name for the log line, or a marker when it cannot be read.

        The name is decoration, not a health check, so it is looked up outside
        the try that guards the handle. A device that will not report its name
        is still a device whose load, temperature and memory can be read, and
        calling that a failure costs the panel three real readings per tick.
        """
        try:
            return str(self._nvml.nvmlDeviceGetName(self._handle))
        except Exception:
            logger.info("NVML device name unavailable, logging it as unnamed", exc_info=True)
            return "unnamed device"

    @property
    def available(self) -> bool:
        return self._handle is not None

    def read(self) -> dict[str, float | None]:
        if self._handle is None:
            return wmi_fallback()
        nvml, handle = self._nvml, self._handle
        out = _empty_gpu()
        try:
            out["gpu_pct"] = float(nvml.nvmlDeviceGetUtilizationRates(handle).gpu)
        except Exception:
            logger.info("NVML utilization unavailable", exc_info=True)
        try:
            out["gpu_temp_c"] = float(
                nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU)
            )
        except Exception:
            logger.info("NVML temperature unavailable", exc_info=True)
        try:
            memory = nvml.nvmlDeviceGetMemoryInfo(handle)
            out["vram_used_gb"] = round(memory.used / _GB, 1)
            out["vram_total_gb"] = round(memory.total / _GB, 1)
        except Exception:
            logger.info("NVML memory info unavailable", exc_info=True)
        return out


class SystemProbe:
    """Builds one Snapshot. Every dependency is injectable for tests."""

    def __init__(
        self,
        cpu_pct: Callable[[], float] | None = None,
        cpu_freq: Callable[[], Any] | None = None,
        ram: Callable[[], Any] | None = None,
        gpu: Any | None = None,
    ) -> None:
        import psutil

        if cpu_pct is None:
            cpu_pct = lambda: psutil.cpu_percent(interval=0.1)  # noqa: E731
        if cpu_freq is None:
            cpu_freq = psutil.cpu_freq
        if ram is None:
            ram = psutil.virtual_memory
        self._cpu_pct = cpu_pct
        self._cpu_freq = cpu_freq
        self._ram = ram
        self._gpu = gpu if gpu is not None else GpuProbe()

    def sample(self) -> Snapshot:
        values: dict[str, float | None] = {}

        try:
            values["cpu_pct"] = float(self._cpu_pct())
        except Exception:
            logger.warning("cpu_percent failed", exc_info=True)

        try:
            freq = self._cpu_freq()
            current = getattr(freq, "current", None)
            maximum = getattr(freq, "max", None)
            values["cpu_mhz"] = float(current) if current else None
            # psutil can report max == 0, and 0 is not a measurement of the
            # CPU's ceiling -- it is dropped rather than passed on as a maximum.
            values["cpu_max_mhz"] = float(maximum) if maximum else None
        except Exception:
            logger.warning("cpu_freq failed", exc_info=True)

        try:
            memory = self._ram()
            values["ram_used_gb"] = round(memory.used / _GB, 1)
            values["ram_total_gb"] = round(memory.total / _GB, 1)
        except Exception:
            logger.warning("virtual_memory failed", exc_info=True)

        try:
            for key, value in self._gpu.read().items():
                if key in GPU_KEYS:
                    values[key] = value
        except Exception:
            logger.warning("gpu read failed", exc_info=True)

        values["ts"] = time.time()
        return Snapshot(**values)
