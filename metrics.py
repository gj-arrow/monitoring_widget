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
# against 6 ms projected. The perf-counter class is the same story, 481 ms
# against 353 ms. A projection naming a property the repository does not have is
# rejected outright rather than returning a row without it, which is why the two
# nominal properties are projected separately: this machine has no
# ProcessorFrequency, so a projection naming both fails as a whole.
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

# The ceiling PercentProcessorPerformance is believed to. It is a percentage of
# the nominal clock: 100 is nominal and above it is turbo. The bound has to clear
# every boost ratio real silicon reaches -- the widest is a ~3.7 GHz Xeon over a
# 2.4 GHz base, about 154% -- while still rejecting values no counter could have
# produced. Nothing clocks a CPU at twice nominal, so 200 is the honest place to
# stop; a plausible turbo figure has to keep working, so this is a ceiling and
# not a target. It is a ceiling only: positivity and finiteness are _speed()'s
# job, so that one gate covers the nominal and this signal alike.
_PERF_RATIO_MAX = 200.0

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
    """One None per field, the shape a source that could not be read returns.

    Takes the key tuple rather than being wrapped per source: the CPU clock and
    the network already call it that way, and a one-line _empty_gpu beside
    them was the odd one out -- a name that had to be kept in step with
    GPU_KEYS by hand for no gain.
    """
    return dict.fromkeys(keys)


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
    out = _empty(GPU_KEYS)
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
        out = _empty(GPU_KEYS)
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


def _speed(value: Any) -> float | None:
    """`value` as a positive, finite number, or None if it is not one.

    One gate for both readings this module derives from a counter, because a
    truthiness test passes a negative, a NaN and an infinity -- all perfectly
    truthy -- and each of those reaches the panel as a fabricated number. 0 is
    included: a driver with nothing to report is not reporting a ceiling, and a
    perf counter's zero is its not-collected-yet sentinel.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0.0 else None


class CpuClockProbe:
    """Nominal clock from WMI; live clock derived from the performance counter.

    One WMI connection for the life of the process, the way GpuProbe holds one
    NVML handle: wmi.WMI() opens a COM connection, so building one on a
    two-second tick means reconnecting 1800 times an hour for a number that
    changes in the hundredths of a megahertz. It is rebuilt on demand when a
    query fails, so a WMI service that restarts mid-session heals instead of
    leaving the clock dashed for the rest of it.

    THE COST, measured over 12 calls on this machine. `SystemProbe.sample()` is
    ~530 ms of wall time and ~140 ms of process CPU, so **7% of a core** -- not
    the 26% an earlier version of this note claimed, which mistook latency for
    throughput. About 70% of the wall figure is this thread *blocked* in the
    WMI provider rather than computing; over the same 12 calls the three WmiPrvSE
    hosts burned ~94 ms between them, which is the provider's own cost and is
    outside this process entirely.

    7% of a core on a worker thread is the price of the only real OS signal for
    the clock, and the GUI is provably unaffected: main.py samples on a QThread
    and the snapshot is only handed to the GUI thread afterwards.

    It is also latency, not throughput, and that is the property that makes it
    acceptable: `ts` is stamped *after* the sample has been taken, so a reading
    that took 530 ms to arrive is labelled 530 ms old rather than being passed
    off as fresh. A panel that rendered a stale number as a current one is the
    bug this probe was written to fix; the cost buys an honest timestamp, not a
    fast one.

    That wall figure is not a floor, and the obvious next reader should know it.
    `ctypes` against `pdh.dll` -- `PdhCollectQueryData` then
    `PdhGetFormattedCounterValue` on
    `\\Processor Information(0,0)\\% Processor Performance` -- returns the same
    value in **~0.018 ms**, stdlib only, no new dependency: roughly 20000x less
    latency for the same signal. It is not a drop-in replacement, and no code
    here attempts it: a wildcard instance array returns `PDH_CSTATUS_INVALID_DATA`,
    the single-instance form needs two collections before the first value is
    valid, and keeping the average over all logical processors means one query
    handle per processor -- twelve on this machine. That is about 100 lines of
    ctypes and a re-derivation of the averaging rule. Tracked as the open
    follow-up in .superpowers/sdd/progress.md.
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
        self._nominal: float | None = None
        self._reconnected = False
        if wmi is not None:
            try:
                self._conn = wmi.WMI()
            except Exception:
                logger.info("WMI unavailable for the CPU clock", exc_info=True)
                self._conn = None

    def _query(self, wql: str) -> list[Any]:
        """Rows for `wql`, rebuilding the connection once if the handle has gone.

        `_query_once` answers None for "this handle did not work", which is not
        the same answer as "no rows" and is what tells the two apart here: the
        first is worth a reconnect, the second is a measurement of nothing.
        """
        rows = self._query_once(wql)
        if rows is not None:
            return rows
        self._reconnect()
        rows = self._query_once(wql)
        return rows if rows is not None else []

    def _query_once(self, wql: str) -> list[Any] | None:
        if self._conn is None:
            return None
        try:
            return list(self._conn.query(wql))
        except Exception:
            logger.info("WMI query failed: %s", wql, exc_info=True)
            return None

    def _reconnect(self) -> None:
        """Replace a handle that has gone stale, at most once per read.

        The connection is cached for the life of the process, and a WMI service
        that restarts mid-session leaves the cached handle unusable: every later
        query fails, the nominal falls back to psutil and the derived clock
        reads -- for the rest of the session however many ticks go by. One
        reconnect per read rather than one per query, because a service that is
        genuinely down should be failed fast instead of being reconnected to
        three times a tick for nothing.
        """
        self._conn = None
        if self._wmi is None or self._reconnected:
            return
        self._reconnected = True
        try:
            self._conn = self._wmi.WMI()
            logger.info("WMI connection rebuilt for the CPU clock")
        except Exception:
            logger.info("WMI still unavailable for the CPU clock", exc_info=True)

    def _nominal_mhz(self) -> float | None:
        """The CPU's nominal clock, in MHz, from whichever source answers.

        Read once and remembered: the ceiling is a property of the installed
        part, so re-asking it every two seconds buys nothing and costs 6.9 ms a
        tick. A *failure* is not remembered, so a provider that was not ready at
        startup -- or a WMI service that comes back later -- is asked again
        rather than leaving the row dashed for the rest of the session.

        A speed is only a speed if it is finite and positive. `if speed:` was a
        truthiness test, and a negative, NaN or infinite ceiling is perfectly
        truthy: -4501 multiplies straight into a negative clock on the panel.
        """
        if self._nominal is not None:
            return self._nominal
        for wql, column in _NOMINAL_SOURCES:
            for row in self._query(wql):
                speed = _speed(getattr(row, column, None))
                if speed is not None:
                    self._nominal = speed
                    return speed
        try:
            fallback = _speed(self._nominal_max())
        except Exception:
            logger.warning("nominal CPU frequency failed", exc_info=True)
            return None
        self._nominal = fallback
        return fallback

    def _performance_ratio(self) -> float | None:
        """Mean PercentProcessorPerformance over the logical processors, in percent.

        This is the ratio of the actual clock to the nominal one, so it is a
        multiplier: 99.2 means 99.2% of nominal. It moves very little -- 99.0
        idle, 99.5 under load -- but it is real OS data, where psutil's current
        is a constant.

        Only the per-processor rows are averaged. `_Total` is a summary *of*
        them, so with no per-processor row there is nothing to average and the
        answer is None. Averaging the summary instead would have been worse than
        nothing: it reads 100 on an idle machine, which multiplies out to the
        nominal exactly and reproduces, byte for byte, the frozen psutil
        constant this probe exists to remove.

        The mean is then held to a plausible band, because a formatted perf
        counter is not a validated measurement and hands over whatever it was
        handed. Without one this code rendered 0 as 0.00 GHz, -42 as -1.89 GHz,
        inf as inf, and 255 as 11.48 GHz on a 4.5 GHz part -- all measured, not
        guessed.
        """
        rows = [
            row for row in self._query(_PERF_RATIO_WQL)
            if _PERF_TOTAL not in str(getattr(row, "Name", ""))
        ]
        ratios = []
        for row in rows:
            raw = getattr(row, "PercentProcessorPerformance", None)
            if raw is None:
                continue  # one processor's counter missing is not a zero reading
            ratio = _speed(raw)
            # _speed has already dropped anything not positive and finite -- NaN
            # and both infinities included -- so all that is left to reject here
            # is a positive value above any clock a CPU can run at. A row outside
            # the bound is skipped rather than voiding the mean, so one garbled
            # core does not cost the panel the other eleven.
            if ratio is not None and ratio <= _PERF_RATIO_MAX:
                ratios.append(ratio)
        if not ratios:
            return None
        return sum(ratios) / len(ratios)

    def read(self) -> dict[str, float | None]:
        self._reconnected = False
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
            moved = now[index] - previous[index]
            if moved < 0.0:
                # The adapter was re-enumerated and its counters restarted, so
                # the bytes since the last sample are unknown rather than zero.
                # Averaging the unknown towards the old figure is the wrong
                # shape of answer: an EMA decays from its peak, and this was
                # measured decaying 300 -> 195 -> 127 -> 82 -> 54 -> 35 -> 23
                # KB/s, six ticks of throughput nobody sent after the link died.
                # A dead average is replaced, not averaged away.
                out[key] = self._seed(key, 0.0)
                continue
            out[key] = self._smooth(key, moved / elapsed)
        return out

    def _seed(self, key: str, raw: float) -> float:
        self._averaged[key] = raw
        return raw

    def _smooth(self, key: str, raw: float) -> float:
        previous = self._averaged[key]
        if previous is None:
            # The first measured sample is the average. Averaging it against
            # nothing would halve the first real figure the panel ever shows.
            return self._seed(key, raw)
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
