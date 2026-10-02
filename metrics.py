"""Reading hardware counters.

One Snapshot per tick. Every field is float | None, where None means "not
measured" and the renderer draws a dash. Nothing in here invents a number:
an estimate that looks like a measurement is worse than no reading at all.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("widget.metrics")

_GB = 1024 ** 3

GPU_KEYS = ("gpu_pct", "gpu_temp_c", "vram_used_gb", "vram_total_gb")
CPU_CLOCK_KEYS = ("cpu_nominal_mhz", "cpu_live_mhz")
NET_KEYS = ("net_down_bytes_per_sec", "net_up_bytes_per_sec")

# Weight of the newest sample in the network average. 0.35 puts the average's
# time constant at about two ticks: a sustained transfer is most of the way on
# screen within two samples, while a single 2-second burst moves the number by
# a third rather than swamping it. Heavier smoothing reads steadier but lags a
# download that starts, and lighter smoothing puts back the jumpiness that made
# the raw delta unreadable as a number.
NET_SMOOTHING = 0.35

# Win32_PerfFormattedData_Counters_ProcessorInformation carries two aggregate
# rows beside the per-processor ones ("_Total" and "0,_Total"). They are
# summaries, not processors, and averaging them in would count the machine
# twice over.
_PERF_TOTAL = "_Total"

# Every WMI query is a projection naming the columns it reads. That is not
# tidiness: wmi's per-class convenience wrapper pulls every property of every
# instance, and Win32_Processor measured 1044 ms that way on this machine
# against 6 ms projected -- half of a two-second tick spent on a hardware
# constant. The perf-counter class is the same story, 481 ms against 353 ms.
# A projection naming a property the repository does not have is rejected
# outright rather than returning a row without it, which is why the ceiling is
# projected under a name this machine actually has.
_PERF_RATIO_WQL = (
    "SELECT Name, PercentProcessorPerformance FROM "
    "Win32_PerfFormattedData_Counters_ProcessorInformation"
)

# (projection, the column it projects), in the order they are tried. Both name
# the nominal clock. MaxClockSpeed is documented as the ceiling and is tried
# first; ProcessorFrequency is documented as the processor's *current* speed, so
# it is a poorer reference and only worth reading when nothing names the ceiling.
_NOMINAL_SOURCES = (
    ("SELECT MaxClockSpeed FROM Win32_Processor", "MaxClockSpeed"),
    ("SELECT ProcessorFrequency FROM Win32_Processor", "ProcessorFrequency"),
)

# Adapters whose name is the only thing that identifies them. The loopback
# pseudo-interface is on every Windows box and its counters move with whatever
# the machine talks to itself about, which is not network throughput.
_PSEUDO_ADAPTERS = ("loopback", "pseudo")


@dataclass(frozen=True, slots=True)
class Snapshot:
    cpu_pct: float | None = None
    cpu_live_mhz: float | None = None
    cpu_nominal_mhz: float | None = None
    ram_used_gb: float | None = None
    ram_total_gb: float | None = None
    gpu_pct: float | None = None
    gpu_temp_c: float | None = None
    vram_used_gb: float | None = None
    vram_total_gb: float | None = None
    net_down_bytes_per_sec: float | None = None
    net_up_bytes_per_sec: float | None = None
    ts: float = 0.0

    # asdict() follows declaration order, which would put ts last. The CSV
    # writer and this list must agree or the trace is silently column-shifted,
    # so the order is stated once here and the header is derived from it.
    CSV_COLUMNS = (
        "ts", "cpu_pct", "cpu_live_mhz", "cpu_nominal_mhz",
        "ram_used_gb", "ram_total_gb",
        "gpu_pct", "gpu_temp_c", "vram_used_gb", "vram_total_gb",
    ) + NET_KEYS

    def as_dict(self) -> dict[str, float | None]:
        return {name: getattr(self, name) for name in self.CSV_COLUMNS}


def _empty(keys) -> dict[str, float | None]:
    return dict.fromkeys(keys)


def _empty_gpu() -> dict[str, float | None]:
    return _empty(GPU_KEYS)


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


def _psutil_nominal_mhz() -> float | None:
    """psutil's own idea of the CPU's ceiling, or None.

    On Windows this is the nominal clock and nothing more: measured on this
    machine three consecutive calls returned current == max == 4501.0 at idle,
    under a 12-thread load and after it, and `percpu` lists a single entry for
    12 logical processors. It is the secondary source for the *reference* only.
    The live clock lives in an MSR a normal process cannot read.
    """
    import psutil

    maximum = getattr(psutil.cpu_freq(), "max", None)
    return float(maximum) if maximum else None


class CpuClockProbe:
    """Nominal clock from WMI; live clock derived from the performance counter.

    One WMI connection for the life of the process, the way GpuProbe holds one
    NVML handle: wmi.WMI() opens a COM connection, so building one on a
    two-second tick means reconnecting 1800 times an hour for a number that
    changes in the hundredths of a megahertz.

    The cost of what is left, measured on this machine over 10 calls: 408 ms per
    read, of which ~350 ms is the perf-counter provider itself and ~6 ms the
    nominal projection. SystemProbe.sample() comes to 524 ms against a 2000 ms
    tick, so this is a 26% duty cycle -- on the sampler's own thread, never the
    GUI's. That is the price of the only real OS signal for the clock, and the
    floor it is measured against is the provider, not this code: the
    unprojected forms of the same two queries cost 1044 ms and 481 ms.
    """

    def __init__(self, wmi: Any | None = None, nominal_max: Callable[[], float | None] | None = None) -> None:
        if wmi is None:
            try:
                import wmi  # type: ignore[no-redef]
            except Exception:
                logger.info("wmi not importable, CPU frequency disabled")
                wmi = None
        self._wmi = wmi
        self._conn: Any | None = None
        self._nominal_max = nominal_max or _psutil_nominal_mhz
        if wmi is not None:
            try:
                self._conn = wmi.WMI()
            except Exception:
                logger.info("WMI unavailable for the CPU clock", exc_info=True)
                self._conn = None

    def _query(self, wql: str) -> list[Any]:
        if self._conn is None:
            return []
        try:
            return list(self._conn.query(wql))
        except Exception:
            logger.info("WMI query failed: %s", wql, exc_info=True)
            return []

    def _nominal_mhz(self) -> float | None:
        """The CPU's nominal clock, in MHz, from whichever source answers."""
        for wql, column in _NOMINAL_SOURCES:
            for row in self._query(wql):
                speed = getattr(row, column, None)
                # 0 is a driver with nothing to report, not a ceiling.
                if speed:
                    return float(speed)
        try:
            fallback = self._nominal_max()
        except Exception:
            logger.warning("nominal CPU frequency failed", exc_info=True)
            return None
        return float(fallback) if fallback else None

    def _performance_ratio(self) -> float | None:
        """Mean PercentProcessorPerformance over the logical processors, in percent.

        This is the ratio of the actual clock to the nominal one, so it is a
        multiplier: 99.2 means 99.2% of nominal. It moves very little -- 99.0
        idle, 99.5 under load -- but it is real OS data, where psutil's current
        is a constant.
        """
        rows = self._query(_PERF_RATIO_WQL)
        logical = [row for row in rows if _PERF_TOTAL not in str(getattr(row, "Name", ""))]
        ratios = []
        for row in logical or rows:
            raw = getattr(row, "PercentProcessorPerformance", None)
            if raw is None:
                continue  # one processor's counter missing is not a zero reading
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isnan(value):
                continue
            ratios.append(value)
        if not ratios:
            return None
        return sum(ratios) / len(ratios)

    def read(self) -> dict[str, float | None]:
        out = _empty(CPU_CLOCK_KEYS)
        nominal = self._nominal_mhz()
        out["cpu_nominal_mhz"] = nominal
        ratio = self._performance_ratio()
        # Both halves are required: a ratio with nothing to multiply is not a
        # clock, and without the ratio there is no live clock to report. The
        # nominal is never published as the live figure -- that is the frozen
        # constant this replaces.
        if nominal is not None and ratio is not None:
            out["cpu_live_mhz"] = round(nominal * ratio / 100.0, 1)
        return out


class NetProbe:
    """Bytes per second, from differencing the cumulative interface counters.

    Holds the previous reading, so one instance per process is the whole state
    this needs: the first sample has no predecessor and is unmeasured rather
    than zero, because "no traffic" and "not measured yet" are different claims.
    """

    def __init__(
        self,
        io_counters: Callable[..., Any] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        if io_counters is None:
            import psutil

            io_counters = psutil.net_io_counters
        self._io_counters = io_counters
        self._now = now or time.time
        self._previous: tuple[float, float, float] | None = None
        self._averaged: dict[str, float | None] = {key: None for key in NET_KEYS}

    def read(self) -> dict[str, float | None]:
        out = _empty(NET_KEYS)
        try:
            totals = _summed_adapters(self._io_counters(pernic=True))
            moment = self._now()
        except Exception:
            logger.warning("net_io_counters failed", exc_info=True)
            return out

        previous = self._previous
        now = (moment, *(totals[key] for key in NET_KEYS))
        # Only on a read that worked. The reference survives an outage on
        # purpose: the counters kept moving while we could not see them, and
        # the bytes over the whole elapsed interval is a true rate, whereas
        # forgetting the reference would cost two dashes for one bad tick.
        self._previous = now
        if previous is None:
            return out
        elapsed = moment - previous[0]
        if elapsed <= 0.0:
            return out  # a rate is bytes over seconds; with no seconds, no rate

        for index, key in enumerate(NET_KEYS, start=1):
            # A counter that went backwards means the adapter was re-enumerated,
            # not that bytes un-transferred: zero is the conservative reading.
            moved = max(0.0, now[index] - previous[index])
            out[key] = self._smooth(key, moved / elapsed)
        return out

    def _smooth(self, key: str, raw: float) -> float:
        previous = self._averaged[key]
        if previous is None:
            # The first measured sample is the average. Averaging it against
            # nothing would halve the first real figure the panel ever shows.
            self._averaged[key] = raw
            return raw
        self._averaged[key] = NET_SMOOTHING * raw + (1.0 - NET_SMOOTHING) * previous
        return self._averaged[key]


def _summed_adapters(counters: Any) -> dict[str, float]:
    """Download and upload bytes so far, summed over the real adapters."""
    download = upload = 0.0
    for name, stats in counters.items():
        if any(token in str(name).lower() for token in _PSEUDO_ADAPTERS):
            continue
        download += float(getattr(stats, "bytes_recv", 0) or 0)
        upload += float(getattr(stats, "bytes_sent", 0) or 0)
    return {"net_down_bytes_per_sec": download, "net_up_bytes_per_sec": upload}


class SystemProbe:
    """Builds one Snapshot. Every dependency is injectable for tests."""

    def __init__(
        self,
        cpu_pct: Callable[[], float] | None = None,
        ram: Callable[[], Any] | None = None,
        gpu: Any | None = None,
        cpu_clock: Any | None = None,
        net: Any | None = None,
    ) -> None:
        import psutil

        if cpu_pct is None:
            cpu_pct = lambda: psutil.cpu_percent(interval=0.1)  # noqa: E731
        if ram is None:
            ram = psutil.virtual_memory
        self._cpu_pct = cpu_pct
        self._ram = ram
        self._gpu = gpu if gpu is not None else GpuProbe()
        self._cpu_clock = cpu_clock if cpu_clock is not None else CpuClockProbe()
        self._net = net if net is not None else NetProbe()

    def sample(self) -> Snapshot:
        values: dict[str, float | None] = {}

        try:
            values["cpu_pct"] = float(self._cpu_pct())
        except Exception:
            logger.warning("cpu_percent failed", exc_info=True)

        try:
            for key, value in self._cpu_clock.read().items():
                if key in CPU_CLOCK_KEYS:
                    values[key] = value
        except Exception:
            logger.warning("cpu clock read failed", exc_info=True)

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

        try:
            for key, value in self._net.read().items():
                if key in NET_KEYS:
                    values[key] = value
        except Exception:
            logger.warning("net read failed", exc_info=True)

        values["ts"] = time.time()
        return Snapshot(**values)
