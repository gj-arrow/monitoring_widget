"""Reading hardware counters.

One Snapshot per tick. Every field is float | None, where None means "not
measured" and the renderer draws a dash. Nothing in here invents a number:
an estimate that looks like a measurement is worse than no reading at all.

There is one WMI connection in this module, and it is behind the GPU fallback:
`_wmi_video_controllers()`, which `GpuProbe` asks at most once a minute on a
machine with no usable NVML handle. The CPU clock used to be the other half of
`wmi`'s reason to be a dependency -- a Win32_Processor projection for the
nominal and Win32_PerfFormattedData_Counters_ProcessorInformation for the ratio
to multiply it by -- and it is gone. Measured, on the machine that decided it:
PercentProcessorPerformance reads 99.1 % of nominal at 9 % load and 99.2 % at
100 %, because a Ryzen 5 5600X drops voltage rather than frequency in
proportion to the work it has been given, so the derived clock moved about 1 %
across a full load sweep while costing 7 % of a core in WMI to read. A reading
that says that was paying a measurable price for a figure nobody could act on.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("widget.metrics")

_GB = 1024 ** 3

GPU_KEYS = ("gpu_pct", "gpu_temp_c", "vram_used_gb", "vram_total_gb")
NET_KEYS = ("net_down_bytes_per_sec", "net_up_bytes_per_sec")

# Weight of the newest sample in the network average. 0.35 puts the average's
# time constant at about two ticks: a sustained transfer is most of the way on
# screen within two samples, while a single 2-second burst moves the number by
# a third rather than swamping it. Heavier smoothing reads steadier but lags a
# download that starts, and lighter smoothing puts back the jumpiness that made
# the raw delta unreadable as a number.
NET_SMOOTHING = 0.35

# Adapters whose name is the only thing that identifies them. The loopback
# pseudo-interface is on every Windows box and its counters move with whatever
# the machine talks to itself about, which is not network throughput.
_PSEUDO_ADAPTERS = ("loopback", "pseudo")

# How long GpuProbe may answer from its cached WMI fallback before asking WMI
# again. Sixty seconds: the answer is a property of the installed card, so it is
# re-read far more slowly than it is displayed, and it is re-read rather than
# cached for good because a WMI service that was down at startup would
# otherwise leave the GPU row dashed for the rest of the session.
WMI_FALLBACK_REFRESH_S = 60.0

# How long a source must be healthy before its next failure is announced. The
# spec's rule for this log is "Логирование ограничено по частоте: не чаще одной
# записи на источник в минуту" -- no more than one record per source per minute
# -- and app_debug.log is capped at 5 KB per ten minutes.
#
# The interval is not a summary timer any more, so it is not the spec's minute.
# It is the healthy spell a source has to serve before a failure counts as news
# again, and the number is set by the ceiling rather than by the spec: a record
# measures 103 B here and its traceback about 340, so 5 KB is roughly 50 records
# per ten minutes, and the six fault sites would have to fit inside that. At the
# spec's minute a source flapping every two minutes still earns a record every
# minute -- measured 6,900 B for five such sources -- so the ceiling was breached
# permanently, not at startup. At 300 s the worst case the rule permits is two
# records per source per ten minutes, and five such sources measure 2,655 B,
# half the ceiling. It is also the floor overlay.PAINT_ERROR_LOG_INTERVAL already
# uses, for the same reason: a repeating fault is one event, not one event per
# interval.
#
# The cost, stated rather than discovered later: a fault of the same type that
# heals and returns inside five minutes is silent. The panel shows that -- the
# row went from a number to dashes and back -- and the traceback for that fault
# is already on file, so what is lost is a timestamp and nothing else.
LOG_INTERVAL = 300.0


@dataclass(slots=True)
class _Fault:
    """What one source has already been given, and what it owes the log next.

    Per source, never shared: five broken sources are five records, and one of
    them healing must not reopen another's silence.
    """

    when: float                      # when the last record was written
    kind: type | None                # the exception type that record named
    traced: bool                     # has this type been given a traceback
    healthy_since: float | None = None  # None while the source is still broken


_FAULT_LOG: dict[str, _Fault] = {}


def _one_line(exc: BaseException) -> str:
    """`TypeName: message`, on one line.

    str(exc) alone drops the type, and the type is the half of an exception
    that a reader searching the log is usually after -- "RuntimeError" versus
    "OSError" says whether to keep reading.
    """
    return f"{type(exc).__name__}: {exc}"


def clear_fault(source: str) -> None:
    """Tell the throttle this source worked. Called on the healthy path.

    Nothing else can: a source that answers does not call log_fault, so this is
    the only way the throttle learns a fault is over, and without it a source
    that recovers can never be announced again -- it would be quiet in the one
    way a broken source is not allowed to be.

    Only the moment is kept, and only the first one: a streak of healthy samples
    is one event, so the streak's start is what a later fault is measured from.
    A source that has never faulted has nothing to say and costs one lookup.
    """
    previous = _FAULT_LOG.get(source)
    if previous is None or previous.healthy_since is not None:
        return
    previous.healthy_since = time.monotonic()


def log_fault(source: str, message: str, exc: BaseException | None = None) -> bool:
    """The first fault for a source, and only what is news after that. True when written.

    Three ways in, and each one is a change rather than a repeat:

    THE FIRST FAULT: always written, with its traceback. Nothing before it.

    A DIFFERENT EXCEPTION TYPE: always written, with a traceback again. The type
    is the one part of a fault that names a different problem -- a dead counter
    and a missing sensor want different fixes, and a log carrying only the first
    one sends the reader after the wrong cause. The traceback's latch resets
    with it, because the *raising line* changes with the exception and a
    traceback that places the old one is a traceback for a different fault.

    A FAULT AFTER A FULL HEALTHY INTERVAL: written, without a traceback (that
    type's is on file). The source's row went from a number to dashes and back,
    which is a fact the log does not otherwise hold.

    And nothing else. A source that keeps failing writes exactly one record for
    the life of the process; a fault whose message varies per occurrence -- two
    adapters alternating, an HRESULT that flips, a counter in the message --
    writes one record, because keying on the message would let any source that
    varies reopen the floor and the bound would not be a bound. Same for a
    source that flaps with a healthy gap short of the interval: a streak that is
    broken by a failure is not a streak, so those healthy samples do not add up
    towards the next record either.
    """
    moment = time.monotonic()
    kind = type(exc) if exc is not None else None
    previous = _FAULT_LOG.get(source)
    changed = previous is not None and previous.kind is not kind
    healed = (
        previous is not None
        and previous.healthy_since is not None
        and moment - previous.healthy_since >= LOG_INTERVAL
    )
    if previous is not None and not (changed or healed):
        # The streak ends here even though nothing is written: this source is
        # broken again, so the next fault starts counting from this moment and
        # not from the last healthy sample before it. Without that, a source
        # flapping every half interval accumulates healthy time across its own
        # failures and eventually earns a recovery record for a spell of health
        # it never had.
        previous.healthy_since = None
        return False

    traced = exc is not None and (previous is None or changed)
    _FAULT_LOG[source] = _Fault(
        when=moment,
        kind=kind,
        traced=traced or (previous.traced if previous is not None else False),
    )

    if exc is None:
        logger.warning("%s", message)
    elif traced:
        logger.warning("%s: %s", message, _one_line(exc), exc_info=exc)
    else:
        logger.warning("%s: %s", message, _one_line(exc))
    return True


@dataclass(frozen=True, slots=True)
class Snapshot:
    cpu_pct: float | None = None
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
        "ts", "cpu_pct",
        "ram_used_gb", "ram_total_gb",
        "gpu_pct", "gpu_temp_c", "vram_used_gb", "vram_total_gb",
    ) + NET_KEYS

    def as_dict(self) -> dict[str, float | None]:
        return {name: getattr(self, name) for name in self.CSV_COLUMNS}


def _empty(keys) -> dict[str, float | None]:
    """One None per field, the shape a source that could not be read returns.

    Takes the key tuple rather than being wrapped per source: the network
    already calls it that way, and a one-line _empty_gpu beside it was the odd
    one out -- a name that had to be kept in step with GPU_KEYS by hand for no
    gain.
    """
    return dict.fromkeys(keys)


def _wmi_video_controllers() -> list[Any]:
    """Win32_VideoController instances, or [] when WMI is unusable.

    The only COM connection this module opens, and it opens it where it queries
    it: `import wmi` runs GetObject("winmgmts:") at module scope, so the first
    import binds a connection to whichever thread asked for it, and
    `wmi.WMI()` binds another to this one. Both belong to the thread that made
    them, so the only thing that keeps this call off the GUI thread is that
    sampling happens on the collector -- see main.py's Collector.
    """
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

    The WMI fallback is held to the same discipline from the other side. With
    no handle, read() used to call wmi_fallback() on every tick, and that opens
    a *fresh* COM connection and runs Win32_VideoController() -- thirty times a
    minute, for an answer that is a property of the installed card. So the
    machines the spec's acceptance criterion 5 singles out as the ones that must
    work were the ones paying for it. The answer is cached and re-checked on
    WMI_FALLBACK_REFRESH_S rather than never re-checked: it can change (a WMI
    service that is down at startup leaves an empty answer cached, and a driver
    install partway through a session would otherwise go unnoticed), and a
    minute is far below anything a person watching the panel would notice.
    """

    def __init__(self, nvml: Any | None = None, now: Callable[[], float] | None = None) -> None:
        if nvml is None:
            try:
                import pynvml as nvml  # type: ignore[no-redef]
            except Exception:
                logger.info("pynvml not importable, GPU metrics disabled")
                nvml = None
        self._nvml = nvml
        self._handle: Any | None = None
        self._now = now or time.monotonic
        self._fallback: dict[str, float | None] | None = None
        self._fallback_at: float = float("-inf")
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
            return self._wmi_fallback()
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

    def _wmi_fallback(self) -> dict[str, float | None]:
        """wmi_fallback(), answered from cache, and a copy of the cache.

        The copy is not tidiness: SystemProbe iterates the dict it is handed
        and copies out the four GPU keys, so handing back the cached object
        would let a caller that added to it change what the next read returns.
        """
        moment = self._now()
        if self._fallback is None or moment - self._fallback_at >= WMI_FALLBACK_REFRESH_S:
            self._fallback = wmi_fallback()
            self._fallback_at = moment
        return dict(self._fallback)


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
        self._previous: tuple[float, frozenset[str], float, float] | None = None
        self._averaged: dict[str, float | None] = {key: None for key in NET_KEYS}

    def read(self) -> dict[str, float | None]:
        out = _empty(NET_KEYS)
        try:
            names, totals = _summed_adapters(self._io_counters(pernic=True))
            moment = self._now()
        except Exception as exc:
            log_fault("net_io_counters", "net_io_counters failed", exc)
            return out
        clear_fault("net_io_counters")

        previous = self._previous
        now = (moment, names, *(totals[key] for key in NET_KEYS))
        # Only on a read that worked. The reference survives an outage on
        # purpose: the counters kept moving while we could not see them, and
        # the bytes over the whole elapsed interval is a true rate, whereas
        # forgetting the reference would cost two dashes for one bad tick.
        self._previous = now
        if previous is None:
            return out
        elapsed = moment - previous[0]
        if names != previous[1]:
            # FIRST, before the elapsed guard below. That ordering is the whole
            # point: the guard returns for a tick where no time passed, and a
            # churn that lands on such a tick is still a churn. With the
            # comparison underneath, an adapter arriving on a backwards clock
            # was never noticed as one -- the tick returned as an ordinary
            # unmeasurable one, the average survived it, and the next tick read
            # 202000 against a true 20000: the same artefact the counter-reset
            # branch was fixed for, reached through its supposed partner.
            #
            # The adapter set changed, so the sum no longer has a term for every
            # interface and this interval cannot be differenced. An adapter that
            # appears brings cumulative counters with it, and summing those in
            # publishes its whole lifetime -- measured with a VPN adapter
            # carrying 9 GB, 1,575,001,000 B/s on one tick, and then six more
            # ticks of the EMA decaying from that peak.
            #
            # Unmeasured is the honest answer, and it is the same answer a
            # counter that went backwards gets below: these are one defect -- a
            # discontinuity in a cumulative counter -- read in two directions.
            # Summing only the adapters present in both samples was the
            # alternative, and it fails on the vanishing side as badly as on the
            # appearing one: the departed adapter's final interval is a term
            # missing from the sum, so the intersection would silently drop real
            # traffic that had already happened. That is the same unbounded
            # error as a lifetime total, not the cosmetic cost of a lost dash --
            # which is what settles set *equality* here rather than a one-way
            # "anything appeared" check. Any number printed is a claim about the
            # machine's total, and the total is unknowable on a tick like this.
            #
            # The average goes with it. Carried across the churn, a 300 KB/s peak
            # blends with the next real reading as 0.35*20000 + 0.65*300000 =
            # 202000 and then decays for five more ticks -- the artefact the
            # counter-reset branch below was fixed for, reached through the branch
            # that claims the two agree. Dropping it costs nothing: the tick after
            # this one seeds fresh from a real measurement.
            #
            # The comparison is over the *real* adapters, because _summed_adapters
            # drops the pseudo interfaces before handing the names back. Windows
            # brings and goes a loopback pseudo-interface on its own, and if it
            # counted here every such churn would cost a tick that was perfectly
            # measurable throughout.
            for key in NET_KEYS:
                self._averaged[key] = None
            return out
        if elapsed <= 0.0:
            # Nothing discontinuous happened on this tick, so the average is
            # still a true description of the link and stays. This is the guard's
            # own case, and it is why the churn comparison is above it rather
            # than merged into it.
            return out  # a rate is bytes over seconds; with no seconds, no rate

        for index, key in enumerate(NET_KEYS, start=2):
            moved = now[index] - previous[index]
            if moved < 0.0:
                # The adapter was re-enumerated and its counters restarted, so
                # the bytes since the last sample are unknown rather than zero.
                # Averaging the unknown towards the old figure is the wrong
                # shape of answer: an EMA decays from its peak, and this was
                # measured decaying 300 -> 195 -> 127 -> 82 -> 54 -> 35 -> 23
                # KB/s, six ticks of throughput nobody sent after the link died.
                # A dead average is dropped so the next sample seeds from a real
                # measurement, and this tick says nothing rather than claiming no
                # traffic moved.
                self._averaged[key] = None
                continue
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


def _summed_adapters(counters: Any) -> tuple[frozenset[str], dict[str, float]]:
    """The real adapters present, and download and upload bytes so far.

    The names come back as well as the totals because the probe has to know
    whether the set is the same one it saw last time: see read(). Pseudo
    interfaces are dropped *before* the set is built, so a loopback appearing or
    vanishing -- which Windows does on its own -- cannot cost a measurable tick.
    """
    names: set[str] = set()
    download = upload = 0.0
    for name, stats in counters.items():
        if any(token in str(name).lower() for token in _PSEUDO_ADAPTERS):
            continue
        names.add(str(name))
        download += float(getattr(stats, "bytes_recv", 0) or 0)
        upload += float(getattr(stats, "bytes_sent", 0) or 0)
    totals = {"net_down_bytes_per_sec": download, "net_up_bytes_per_sec": upload}
    return frozenset(names), totals


class SystemProbe:
    """Builds one Snapshot. Every dependency is injectable for tests.

    Four sources, each with its own guard, so one dead sensor costs its own row
    rather than the panel. `ts` is stamped last, after every reading has been
    taken, which is what makes it a statement about the whole sample rather than
    about its start.
    """

    def __init__(
        self,
        cpu_pct: Callable[[], float] | None = None,
        ram: Callable[[], Any] | None = None,
        gpu: Any | None = None,
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
        self._net = net if net is not None else NetProbe()

    def sample(self) -> Snapshot:
        values: dict[str, float | None] = {}

        try:
            values["cpu_pct"] = float(self._cpu_pct())
            clear_fault("cpu_percent")
        except Exception as exc:
            log_fault("cpu_percent", "cpu_percent failed", exc)

        try:
            memory = self._ram()
            values["ram_used_gb"] = round(memory.used / _GB, 1)
            values["ram_total_gb"] = round(memory.total / _GB, 1)
            clear_fault("virtual_memory")
        except Exception as exc:
            log_fault("virtual_memory", "virtual_memory failed", exc)

        try:
            for key, value in self._gpu.read().items():
                if key in GPU_KEYS:
                    values[key] = value
            clear_fault("gpu read")
        except Exception as exc:
            log_fault("gpu read", "gpu read failed", exc)

        try:
            for key, value in self._net.read().items():
                if key in NET_KEYS:
                    values[key] = value
            clear_fault("net read")
        except Exception as exc:
            log_fault("net read", "net read failed", exc)

        values["ts"] = time.time()
        return Snapshot(**values)
