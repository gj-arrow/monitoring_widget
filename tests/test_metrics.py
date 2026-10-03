import logging
from dataclasses import fields
from types import SimpleNamespace

import pytest

import metrics
from metrics import CpuClockProbe, GpuProbe, NetProbe, Snapshot, SystemProbe, wmi_fallback

GB = 1024 ** 3


class FakeUtil:
    def __init__(self, gpu):
        self.gpu = gpu


class FakeMemory:
    def __init__(self, used, total):
        self.used = used
        self.total = total


class FakeNvml:
    """Stands in for the pynvml module and counts every call."""

    NVML_TEMPERATURE_GPU = 0

    def __init__(self, fail_init=False, fail_name=False, fail_on=()):
        """`fail_on` names the reads to break: "util", "temp", "mem"."""
        self.calls = {}
        self._fail_init = fail_init
        self._fail_name = fail_name
        self._fail_on = frozenset(fail_on)

    def _count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def nvmlInit(self):
        self._count("nvmlInit")
        if self._fail_init:
            raise RuntimeError("no device")

    def nvmlDeviceGetHandleByIndex(self, index):
        self._count("handle")
        return f"handle-{index}"

    def nvmlDeviceGetName(self, handle):
        self._count("name")
        if self._fail_name:
            raise RuntimeError("name lookup is broken")
        return "Fake GPU"

    def nvmlDeviceGetUtilizationRates(self, handle):
        self._count("util")
        if "util" in self._fail_on:
            raise RuntimeError("utilization is unavailable")
        return FakeUtil(42)

    def nvmlDeviceGetTemperature(self, handle, sensor):
        self._count("temp")
        if "temp" in self._fail_on:
            raise RuntimeError("temperature sensor is unavailable")
        return 61

    def nvmlDeviceGetMemoryInfo(self, handle):
        self._count("mem")
        if "mem" in self._fail_on:
            raise RuntimeError("memory info is unavailable")
        return FakeMemory(2 * GB, 8 * GB)


class BrokenGpu:
    """A GPU probe whose read fails the way a vanishing driver does."""

    def read(self):
        raise RuntimeError("the driver went away")


class FakeGpu:
    def __init__(self, values):
        self.values = values

    def read(self):
        return dict(self.values)


class FakeCpuClock:
    def __init__(self, values):
        self.values = values
        self.reads = 0

    def read(self):
        self.reads += 1
        return dict(self.values)


class BrokenCpuClock:
    """A clock source that dies the way a stopped WMI service does."""

    def read(self):
        raise RuntimeError("the WMI service has stopped")


class FakeWmiConnection:
    """The two Win32 classes CpuClockProbe queries, dispatched by WQL.

    Dispatching on the query rather than on a per-class convenience method is
    how the real client behaves, and it matters: a WQL projection naming a
    property the repository does not have is rejected outright rather than
    quietly returning a row without it. That is exactly what this machine does
    with Win32_Processor.ProcessorFrequency.
    """

    def __init__(self, processors=(), perf=(), absent=("ProcessorFrequency",), fail=False):
        self._processors = processors
        self._perf = perf
        self._absent = absent
        self.fail = fail
        self.queries = []

    def query(self, wql):
        self.queries.append(wql)
        if self.fail:
            raise RuntimeError("the WMI service has stopped")
        for name in self._absent:
            if name in wql:
                raise RuntimeError(f"invalid query: {name} is not a property")
        if "PercentProcessorPerformance" in wql:
            return list(self._perf)
        # Every other projection gets the same instances back, unfiltered: a real
        # projection returns the row with only the column it named, so dropping
        # a row here would quietly do the probe's own deciding for it. What the
        # row does not carry has to come back as a missing attribute.
        return list(self._processors)

    def Win32_Processor(self):
        raise AssertionError(
            "the unprojected Win32_Processor fetch measured 1044 ms on this "
            "machine against a 6 ms projection; the nominal clock has to come "
            "from a projection or it eats half of every tick"
        )

    def Win32_PerfFormattedData_Counters_ProcessorInformation(self):
        raise AssertionError(
            "the unprojected perf-counter fetch measured 481 ms on this machine "
            "against 353 ms projected; the counter has to come from a projection"
        )


class FakeWmi:
    """Stands in for the wmi module and counts every connection it builds."""

    def __init__(self, connection=None, fail_connect=False):
        self.connects = 0
        self._connection = connection
        self._fail_connect = fail_connect
        self.down = False

    def WMI(self):
        self.connects += 1
        if self._fail_connect or self.down:
            raise RuntimeError("the WMI service has stopped")
        self._connection.fail = False   # a fresh handle is not a stale one
        return self._connection


def perf_rows(*ratios):
    """One Win32_PerfFormattedData_Counters_ProcessorInformation row per ratio."""
    return [
        SimpleNamespace(Name=f"0,{index}", PercentProcessorPerformance=ratio)
        for index, ratio in enumerate(ratios)
    ]


class FakeCounters:
    """psutil.net_io_counters(pernic=True) with a scripted series per call."""

    def __init__(self, samples):
        self.samples = [list(sample) for sample in samples]
        self.calls = 0

    def __call__(self, pernic=True):
        # The last sample repeats rather than raising: read() swallows counter
        # failures, so an exhausted script would turn into a silent None here.
        sample = self.samples[min(self.calls, len(self.samples) - 1)]
        self.calls += 1
        return {
            name: SimpleNamespace(bytes_recv=recv, bytes_sent=sent)
            for name, recv, sent in sample
        }


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_probe_initialises_nvml_exactly_once():
    nvml = FakeNvml()
    probe = GpuProbe(nvml=nvml)
    probe.read()
    probe.read()
    probe.read()
    assert nvml.calls["nvmlInit"] == 1
    assert nvml.calls["handle"] == 1


def test_probe_reads_every_gpu_number_in_one_pass():
    probe = GpuProbe(nvml=FakeNvml())
    assert probe.read() == {
        "gpu_pct": 42.0,
        "gpu_temp_c": 61.0,
        "vram_used_gb": 2.0,
        "vram_total_gb": 8.0,
    }


def test_probe_reports_unavailable_when_nvml_cannot_start(monkeypatch, caplog):
    monkeypatch.setattr(metrics, "_wmi_video_controllers", lambda: [])
    with caplog.at_level(logging.INFO, logger="widget.metrics"):
        probe = GpuProbe(nvml=FakeNvml(fail_init=True))
    assert probe.available is False
    assert probe.read() == {
        "gpu_pct": None,
        "gpu_temp_c": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
    }
    # Genuinely no device: the log has to say so, not merely stay quiet.
    assert any("falling back to WMI" in r.getMessage() for r in caplog.records)


def test_probe_stays_available_when_only_the_device_name_lookup_fails(caplog):
    # The name is a log line, not a health check. Guarding it together with the
    # handle acquisition means a device that merely refuses to name itself is
    # declared dead: every tick then falls back to WMI and the panel loses a
    # real load percentage and a real temperature it was perfectly able to read.
    with caplog.at_level(logging.INFO, logger="widget.metrics"):
        probe = GpuProbe(nvml=FakeNvml(fail_name=True))

    assert probe.available is True
    assert probe.read() == {
        "gpu_pct": 42.0,
        "gpu_temp_c": 61.0,
        "vram_used_gb": 2.0,
        "vram_total_gb": 8.0,
    }
    messages = [r.getMessage() for r in caplog.records]
    assert not any("falling back to WMI" in m for m in messages)
    # An unnamed device, stated as such: the name is missing, not the device.
    assert any("unnamed" in m for m in messages)


def test_wmi_fallback_does_not_invent_a_load_percentage(monkeypatch):
    # 1 GB is the largest size WMI can actually report: AdapterRAM is a signed
    # int32, so 2 GB and up wraps negative (see the test below). The old
    # fixture said 8 GB, a value no real machine can return, so the happy path
    # was covered by a test that could not happen and the real one went unseen.
    controller = SimpleNamespace(AdapterRAM=1 * GB, CurrentClockFrequency=900, MaxClockSpeed=1800)
    monkeypatch.setattr(metrics, "_wmi_video_controllers", lambda: [controller])
    result = wmi_fallback()
    assert result["gpu_pct"] is None
    assert result["gpu_temp_c"] is None
    assert result["vram_used_gb"] is None
    assert result["vram_total_gb"] == 1.0


@pytest.mark.parametrize("adapter_ram", [-1048576, 0, None])
def test_wmi_fallback_reports_a_wrapped_adapter_ram_as_unmeasured(monkeypatch, adapter_ram):
    # -1048576 is 0xFF000000, what this machine's 12 GB RTX 3060 actually
    # reports: the real size overflowed the signed int32 and came back
    # negative. Dividing it produced -0.0 here and -1.0 for an 8 GB card --
    # invented numbers wearing the costume of a measurement. A size that is not
    # strictly positive was never measured and has to come back as None.
    controller = SimpleNamespace(AdapterRAM=adapter_ram)
    monkeypatch.setattr(metrics, "_wmi_video_controllers", lambda: [controller])
    assert wmi_fallback()["vram_total_gb"] is None


def test_wmi_fallback_handles_no_controllers(monkeypatch):
    monkeypatch.setattr(metrics, "_wmi_video_controllers", lambda: [])
    assert wmi_fallback()["vram_total_gb"] is None


def test_sample_fills_every_field():
    probe = SystemProbe(
        cpu_pct=lambda: 12.5,
        cpu_clock=FakeCpuClock({"cpu_nominal_mhz": 4501.0, "cpu_live_mhz": 4464.0}),
        ram=lambda: SimpleNamespace(used=11.4 * GB, total=32 * GB),
        gpu=FakeGpu({"gpu_pct": 61.0, "gpu_temp_c": 58.0, "vram_used_gb": 6.2, "vram_total_gb": 20.0}),
        net=FakeGpu({}),
    )
    snap = probe.sample()
    assert snap.cpu_pct == 12.5
    assert snap.cpu_live_mhz == 4464.0
    assert snap.cpu_nominal_mhz == 4501.0
    assert snap.ram_used_gb == 11.4
    assert snap.ram_total_gb == 32.0
    assert snap.gpu_pct == 61.0
    assert snap.gpu_temp_c == 58.0
    assert snap.vram_used_gb == 6.2
    assert snap.vram_total_gb == 20.0
    # The first tick has no predecessor to difference against, so the rate is
    # unmeasured rather than zero.
    assert snap.net_down_bytes_per_sec is None
    assert snap.net_up_bytes_per_sec is None
    assert snap.ts > 0.0


def test_sample_drops_a_zero_nominal_frequency():
    probe = CpuClockProbe(
        wmi=FakeWmi(FakeWmiConnection(processors=[SimpleNamespace(MaxClockSpeed=0)], perf=[])),
        nominal_max=lambda: 0.0,
    )
    # 0 is not a measurement of the CPU's ceiling -- it is a driver that has
    # nothing to say, and dividing by it or displaying it would be inventing one.
    assert probe.read() == {"cpu_nominal_mhz": None, "cpu_live_mhz": None}


def test_sample_reports_none_when_a_source_raises():
    def boom():
        raise RuntimeError("sensor bus is on fire")

    probe = SystemProbe(
        cpu_pct=boom, cpu_clock=BrokenCpuClock(), ram=boom,
        gpu=FakeGpu({}), net=FakeGpu({}),
    )
    snap = probe.sample()
    assert snap.cpu_pct is None
    assert snap.cpu_live_mhz is None
    assert snap.cpu_nominal_mhz is None
    assert snap.ram_used_gb is None
    assert snap.ram_total_gb is None
    assert snap.gpu_pct is None
    assert snap.ts > 0.0


@pytest.mark.parametrize("failing", ["cpu_pct", "cpu_clock", "ram", "net"])
def test_sample_isolates_one_failing_source(failing):
    # The test above breaks every source at once, so it cannot tell five
    # separate guards from one: collapsing them into a single try would leave
    # it green while one dead sensor blanked the whole panel. One source fails
    # here and every other reading must survive.
    dependencies = {
        "cpu_pct": lambda: 12.5,
        "ram": lambda: SimpleNamespace(used=11.4 * GB, total=32 * GB),
        "cpu_clock": FakeCpuClock({"cpu_nominal_mhz": 4501.0, "cpu_live_mhz": 4464.0}),
        "net": FakeGpu({"net_down_bytes_per_sec": 15000.0, "net_up_bytes_per_sec": 30000.0}),
    }
    readable = {
        "cpu_pct": 12.5,
        "cpu_live_mhz": 4464.0,
        "cpu_nominal_mhz": 4501.0,
        "ram_used_gb": 11.4,
        "ram_total_gb": 32.0,
        "net_down_bytes_per_sec": 15000.0,
        "net_up_bytes_per_sec": 30000.0,
    }
    # The clock source fills two fields, so it takes both down with it.
    lost = {
        "cpu_pct": ("cpu_pct",),
        "cpu_clock": ("cpu_live_mhz", "cpu_nominal_mhz"),
        "ram": ("ram_used_gb", "ram_total_gb"),
        "net": ("net_down_bytes_per_sec", "net_up_bytes_per_sec"),
    }[failing]

    def boom():
        raise RuntimeError("sensor bus is on fire")

    dependencies[failing] = _broken_source() if failing in ("cpu_clock", "net") else boom
    snap = SystemProbe(gpu=FakeGpu({}), **dependencies).sample()

    for field, value in readable.items():
        if field in lost:
            assert getattr(snap, field) is None, field
        else:
            assert getattr(snap, field) == value, field
    assert snap.ts > 0.0


def _broken_source():
    class Broken:
        def read(self):
            raise RuntimeError("sensor bus is on fire")

    return Broken()


def test_sample_isolates_a_failing_gpu_read():
    # The GPU is one source among several and the one most likely to die with
    # the driver, so nothing required the CPU, RAM and network readings to
    # survive its failure: merging the ram and gpu guards left every test in
    # this file green while one missing driver blanked the other rows too.
    probe = SystemProbe(
        cpu_pct=lambda: 12.5,
        cpu_clock=FakeCpuClock({"cpu_nominal_mhz": 4501.0, "cpu_live_mhz": 4464.0}),
        ram=lambda: SimpleNamespace(used=11.4 * GB, total=32 * GB),
        gpu=BrokenGpu(),
        net=FakeGpu({"net_down_bytes_per_sec": 15000.0, "net_up_bytes_per_sec": 30000.0}),
    )
    snap = probe.sample()
    assert snap.cpu_pct == 12.5
    assert snap.cpu_live_mhz == 4464.0
    assert snap.cpu_nominal_mhz == 4501.0
    assert snap.ram_used_gb == 11.4
    assert snap.ram_total_gb == 32.0
    assert snap.gpu_pct is None
    assert snap.net_down_bytes_per_sec == 15000.0
    assert snap.net_up_bytes_per_sec == 30000.0
    assert snap.ts > 0.0


@pytest.mark.parametrize(
    "failing,field,expected",    [
        ("util", "gpu_pct",
         {"gpu_pct": None, "gpu_temp_c": 61.0, "vram_used_gb": 2.0, "vram_total_gb": 8.0}),
        ("temp", "gpu_temp_c",
         {"gpu_pct": 42.0, "gpu_temp_c": None, "vram_used_gb": 2.0, "vram_total_gb": 8.0}),
        ("mem", "vram_used_gb",
         {"gpu_pct": 42.0, "gpu_temp_c": 61.0, "vram_used_gb": None, "vram_total_gb": None}),
    ],
)
def test_probe_isolates_one_failing_nvml_read(failing, field, expected):
    # Same gap in the GPU probe: three reads, three guards, and nothing said
    # so. Each case states the readings that must survive, and the dead one
    # must be None -- reporting 0% load or 0 GB would be a fabricated reading.
    probe = GpuProbe(nvml=FakeNvml(fail_on=(failing,)))
    values = probe.read()
    assert values == expected
    assert values[field] is None


def test_snapshot_as_dict_round_trips():
    snap = Snapshot(cpu_pct=5.0)
    assert snap.as_dict()["cpu_pct"] == 5.0
    assert snap.as_dict()["gpu_temp_c"] is None


# --- the CPU clock -------------------------------------------------------


def clock_probe(perf=(), processors=(), nominal=4500.0, connect_fails=False, absent=("ProcessorFrequency",)):
    """A CpuClockProbe on a fake COM connection, built the way the app does."""
    return CpuClockProbe(
        wmi=FakeWmi(FakeWmiConnection(processors=processors, perf=perf, absent=absent),
                    fail_connect=connect_fails),
        nominal_max=lambda: nominal,
    )


def test_cpu_clock_probe_builds_one_wmi_connection_for_the_whole_process():
    # wmi.WMI() opens a COM connection and holds it. Calling it per tick means a
    # fresh connect every two seconds for as long as the panel is up, which is
    # exactly the waste GpuProbe avoids by holding one NVML handle.
    wmi = FakeWmi(FakeWmiConnection(
        processors=[SimpleNamespace(MaxClockSpeed=4501)], perf=perf_rows(99.0),
    ))
    probe = CpuClockProbe(wmi=wmi, nominal_max=lambda: None)
    for _ in range(20):
        probe.read()
    assert wmi.connects == 1


def test_the_live_clock_is_the_nominal_times_the_average_performance_ratio():
    # Win32_PerfFormattedData_Counters_ProcessorInformation.PercentProcessor-
    # Performance is the ratio of the actual clock to the nominal one, so it is
    # a multiplier and not a reading: the clock is nominal * ratio / 100. The
    # three ratios here are the ones measured on this machine -- 99.0 idle,
    # 99.5 under load -- and their mean, 99.2, gives 4500 * 0.992 = 4464.0.
    probe = clock_probe(perf=perf_rows(99.0, 99.5, 99.1),
                        processors=[SimpleNamespace(MaxClockSpeed=4500)])
    values = probe.read()
    assert values == {"cpu_nominal_mhz": 4500.0, "cpu_live_mhz": 4464.0}
    # And it is a derivation, not the nominal passed through under a new name:
    # the old code filled the row with a frozen 4501 that looked measured.
    assert values["cpu_live_mhz"] != values["cpu_nominal_mhz"]


def test_the_live_clock_averages_the_logical_processors_and_ignores_the_total_rows():
    # The class carries two aggregate rows beside the per-processor ones --
    # "_Total" and "0,_Total" on this 12-thread machine. Averaging those in
    # counts the machine twice over and lets one aggregate value steer the
    # reading, so 4464 would become 4114.
    perf = perf_rows(*([100.0] * 12))
    perf += [SimpleNamespace(Name="_Total", PercentProcessorPerformance=40.0),
             SimpleNamespace(Name="0,_Total", PercentProcessorPerformance=40.0)]
    probe = clock_probe(perf=perf, processors=[SimpleNamespace(MaxClockSpeed=4500)])
    assert probe.read()["cpu_live_mhz"] == 4500.0


def test_the_live_clock_is_unmeasured_when_the_performance_counter_is_unavailable():
    # The nominal is known, so filling the live field with it would look
    # correct on screen and be a lie: it is the same frozen constant the panel
    # has always shown. A counter that will not answer means no live reading.
    probe = clock_probe(perf=[], processors=[SimpleNamespace(MaxClockSpeed=4501)])
    assert probe.read() == {"cpu_nominal_mhz": 4501.0, "cpu_live_mhz": None}


def test_the_live_clock_uses_whatever_processors_did_answer():
    # One processor whose ratio never arrived must not be read as zero: that
    # would halve the average and understate the clock on a machine where a
    # single core's counter is unavailable.
    perf = [SimpleNamespace(Name="0,0", PercentProcessorPerformance=99.0),
            SimpleNamespace(Name="0,1")]
    probe = clock_probe(perf=perf, processors=[SimpleNamespace(MaxClockSpeed=4500)])
    assert probe.read()["cpu_live_mhz"] == 4455.0


def test_the_live_clock_cannot_be_derived_without_a_nominal_reference():
    # A ratio with nothing to multiply is not a clock. Reporting the ratio as
    # one, or as 100% of nothing, would be a number nobody measured.
    probe = clock_probe(perf=perf_rows(99.0), processors=[], nominal=None)
    assert probe.read() == {"cpu_nominal_mhz": None, "cpu_live_mhz": None}


def test_the_nominal_clock_falls_back_to_psutil_when_wmi_has_nothing_to_say():
    # psutil.cpu_freq().max is the same quantity on Windows -- the nominal
    # clock -- so it is a second source for the reference and never for the
    # live reading. MaxClockSpeed 0 is dropped for the reason its psutil twin
    # is: zero is a driver with nothing to report, not a ceiling.
    probe = clock_probe(perf=perf_rows(99.0),
                        processors=[SimpleNamespace(MaxClockSpeed=0)], nominal=4500.0)
    assert probe.read() == {"cpu_nominal_mhz": 4500.0, "cpu_live_mhz": 4455.0}


def test_the_nominal_clock_comes_from_a_projection_and_not_the_full_fetch():
    # Two measured facts force this. wmi's per-class convenience wrapper pulls
    # every property of every instance: Win32_Processor took 1044 ms that way on
    # this machine and 6 ms as a projection naming one column -- half of a
    # two-second tick spent on a hardware constant that cannot change while the
    # process runs. And a projection naming ProcessorFrequency is rejected
    # outright by a repository that lacks it, which is every machine measured
    # here, so the ceiling has to be projected under a name that is there. The
    # fake raises on both unprojected wrappers, so a revert is a failure with
    # the measurement in the message rather than a slow test nobody notices.
    probe = clock_probe(perf=perf_rows(99.0),
                        processors=[SimpleNamespace(MaxClockSpeed=4501)])
    assert probe.read()["cpu_nominal_mhz"] == 4501.0


def test_the_nominal_clock_uses_processor_frequency_when_max_clock_speed_is_absent():
    # Both names for the reference exist in the class and a provider may answer
    # to only one of them, so the second is tried when the first says nothing.
    # ProcessorFrequency is the *last* source and not the first: it is
    # documented as the processor's current speed, which is a poorer ceiling
    # than MaxClockSpeed and the same confusion this task is removing.
    probe = clock_probe(perf=perf_rows(99.0),
                        processors=[SimpleNamespace(ProcessorFrequency=4200)],
                        nominal=None, absent=())
    assert probe.read()["cpu_nominal_mhz"] == 4200.0


def test_the_cpu_clock_probe_survives_a_wmi_service_that_will_not_connect(caplog):
    with caplog.at_level(logging.INFO, logger="widget.metrics"):
        probe = clock_probe(perf=perf_rows(99.0),
                            processors=[SimpleNamespace(MaxClockSpeed=4501)],
                            nominal=4500.0, connect_fails=True)
    # The secondary source does not need WMI, so the nominal still arrives; the
    # derived clock does, and is therefore unmeasured rather than nominal.
    assert probe.read() == {"cpu_nominal_mhz": 4500.0, "cpu_live_mhz": None}
    messages = [r.getMessage() for r in caplog.records]
    assert any("WMI" in m for m in messages), (
        f"a WMI connection that could not be built logged nothing: {messages}"
    )


def test_the_cpu_clock_probe_survives_a_query_that_raises():
    class Exploding(FakeWmiConnection):
        def query(self, wql):
            raise RuntimeError("WQL error")

    probe = CpuClockProbe(wmi=FakeWmi(Exploding()), nominal_max=lambda: 4500.0)
    assert probe.read() == {"cpu_nominal_mhz": 4500.0, "cpu_live_mhz": None}


def test_the_cpu_clock_probe_survives_a_repository_that_rejects_every_projection():
    # A projection can be refused where the unprojected wrapper is not, so the
    # secondary source has to be reachable without WMI at all. Otherwise a
    # machine whose provider rejects the query loses the nominal for good --
    # the derived clock with it -- and the row shows a dash forever.
    class Rejecting(FakeWmiConnection):
        def query(self, wql):
            raise RuntimeError("invalid query")

    probe = CpuClockProbe(wmi=FakeWmi(Rejecting()), nominal_max=lambda: 4500.0)
    assert probe.read() == {"cpu_nominal_mhz": 4500.0, "cpu_live_mhz": None}


# --- network throughput --------------------------------------------------


def net_probe(samples, start=100.0):
    return NetProbe(io_counters=FakeCounters(samples), now=FakeClock(start))


def test_the_first_network_sample_has_no_predecessor_and_is_unmeasured():
    # Cumulative counters need a delta, and the first tick has nothing to
    # difference against. Reporting 0 B/s here would say "no traffic" when the
    # honest answer is "not measured yet" -- the two are different claims.
    probe = net_probe([[("Ethernet", 1_000_000, 2_000_000)]])
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}


def test_the_first_measured_rate_seeds_the_average_instead_of_diluting_it():
    probe = net_probe([[("Ethernet", 0, 0)], [("Ethernet", 30_000, 60_000)]])
    probe.read()
    probe._now.advance(2.0)
    # 30000 bytes over 2 s is 15000 B/s. Averaging that against a zero that was
    # never measured would halve the first real figure the panel ever shows.
    assert probe.read() == {"net_down_bytes_per_sec": 15000.0, "net_up_bytes_per_sec": 30000.0}


def test_later_rates_are_an_exponential_moving_average_of_the_raw_delta():
    # A raw two-second delta of zero is not "the link went dead": it is one
    # quiet window. The average is 35% of the new sample and 65% of the last,
    # so a single quiet tick pulls a 1000 B/s reading down to 650 and never to
    # 0. Raw deltas give 0 here, a two-sample mean gives 500, and a 0.99 factor
    # gives 10 -- only the documented weight gives 650.
    probe = net_probe([[("Ethernet", 0, 0)],
                       [("Ethernet", 2_000, 2_000)],
                       [("Ethernet", 2_000, 2_000)]])
    probe.read()
    probe._now.advance(2.0)
    assert probe.read()["net_down_bytes_per_sec"] == 1000.0
    probe._now.advance(2.0)
    assert probe.read()["net_down_bytes_per_sec"] == 650.0


def test_the_rate_uses_the_measured_interval_rather_than_the_tick_period():
    # The tick is 2000 ms in theme, but a tick that arrives late has to divide
    # by the time that actually passed. 30000 bytes over 1.5 s is 20000 B/s;
    # a hardcoded 2 s divisor would report 15000 and a 4 s interval 7500.
    clock = FakeClock(100.0)
    probe = NetProbe(io_counters=FakeCounters([[("Ethernet", 0, 0)],
                                               [("Ethernet", 30_000, 30_000)]]), now=clock)
    probe.read()
    clock.advance(1.5)
    assert probe.read()["net_down_bytes_per_sec"] == 20_000.0


def test_loopback_and_pseudo_interfaces_are_excluded_from_the_totals():
    # Every Windows box carries "Loopback Pseudo-Interface 1" and its counters
    # move with whatever the machine talks to itself about. Its name is the only
    # thing that identifies it -- there is no link-layer identity to check --
    # so the exclusion has to be by name, and it has to actually bite: here the
    # loopback is ten million bytes against the adapter's thirty thousand.
    probe = net_probe([[("Ethernet", 0, 0)],
                       [("Ethernet", 30_000, 30_000),
                        ("Loopback Pseudo-Interface 1", 10_000_000, 10_000_000)]])
    probe.read()
    probe._now.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": 15_000.0, "net_up_bytes_per_sec": 15_000.0}


def test_real_adapters_are_summed_rather_than_one_of_them_winning():
    # Two adapters carrying a download and an upload at once is the ordinary
    # case on a laptop with a dock and a wifi radio, and the panel has one
    # number per direction, so both have to be in it.
    probe = net_probe([[("Ethernet", 0, 0), ("Wi-Fi", 0, 0)],
                       [("Ethernet", 30_000, 0), ("Wi-Fi", 10_000, 6_000)]])
    probe.read()
    probe._now.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": 20_000.0, "net_up_bytes_per_sec": 3_000.0}


def test_the_rate_is_unmeasured_when_no_time_passed_between_two_samples():
    # A rate is bytes over seconds; with no seconds there is no rate. Dividing
    # anyway raises ZeroDivisionError or reports an infinite throughput, and the
    # counters just read become the reference for the next interval either way.
    clock = FakeClock(100.0)
    probe = NetProbe(io_counters=FakeCounters([[("Ethernet", 0, 0)],
                                               [("Ethernet", 30_000, 30_000)],
                                               [("Ethernet", 60_000, 60_000)]]), now=clock)
    probe.read()
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}
    clock.advance(2.0)
    # The stalled sample did not accumulate: 60000 over 2 s, not 90000.
    assert probe.read()["net_down_bytes_per_sec"] == 15_000.0


def test_a_counter_that_went_backwards_reads_as_unmeasured_not_as_zero():
    # Adapters are re-enumerated and counters restart. The bytes did not
    # un-transfer, so the amount is unknown -- and unknown is what this module
    # renders as a dash everywhere else. Seeding the average with 0.0 here said
    # "nothing moved", which is a different claim, and the seed then decayed from
    # the pre-reset peak for six ticks: throughput nobody sent, in the panel and
    # in the CSV.
    probe = net_probe([[("Ethernet", 900_000, 900_000)],
                       [("Ethernet", 0, 0)]])
    probe.read()
    probe._now.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}


def test_a_failing_counter_read_leaves_the_next_sample_measured():
    # A source that dies has not told us anything, so no rate that tick. The
    # counters behind it kept moving while we could not see them, so the
    # sample after the outage is still a true rate over the whole gap --
    # forgetting the reference instead would cost two dashes for one bad tick.
    clock = FakeClock(100.0)
    state = {"calls": 0}

    def flaky(pernic=True):
        state["calls"] += 1
        if state["calls"] == 2:
            raise RuntimeError("the network stack is restarting")
        moved = 0 if state["calls"] == 1 else 30_000
        return {"Ethernet": SimpleNamespace(bytes_recv=moved, bytes_sent=moved)}

    probe = NetProbe(io_counters=flaky, now=clock)
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}
    clock.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}
    clock.advance(4.0)
    # 30000 bytes across the six seconds since the last counters we actually
    # saw, which is the rate. Dividing by one tick instead would report 15000.
    assert probe.read()["net_down_bytes_per_sec"] == 5_000.0


def test_the_network_probe_samples_the_counters_once_per_read():
    # One psutil call per tick: the point of the probe is a delta, and reading
    # twice inside one read would difference a counter against itself.
    counters = FakeCounters([[("Ethernet", 0, 0)], [("Ethernet", 30_000, 30_000)]])
    probe = NetProbe(io_counters=counters, now=FakeClock(100.0))
    probe.read()
    probe._now.advance(2.0)
    probe.read()
    assert counters.calls == 2


def test_aggregate_rows_are_not_a_substitute_for_the_logical_processors():
    """`_Total` is a summary of the per-processor rows, not a processor.

    When it is the only row that comes back it reads 100 -- exactly nominal --
    and publishing it as the live clock reproduces, byte for byte, the frozen
    psutil constant this whole probe exists to remove. That branch had no test:
    the one test with an empty perf list produces `[] or []`, which never
    reaches it.
    """
    for name in ("_Total", "0,_Total"):
        probe = clock_probe(
            perf=[SimpleNamespace(Name=name, PercentProcessorPerformance=100)],
            processors=[SimpleNamespace(MaxClockSpeed=4501)],
        )
        assert probe.read() == {"cpu_nominal_mhz": 4501.0, "cpu_live_mhz": None}, name


def test_a_genuine_hundred_percent_average_is_still_allowed_to_equal_the_nominal():
    """The counterpart to the aggregate test, and the reason it is not a lookup.

    live == nominal is not by itself the bug: on a boosted part the mean over
    the logical processors really does land on 100.0, and then the derived clock
    and the reference agree. What made the old code wrong was arriving there from
    an aggregate row. Measured on this machine, a sample under load does produce
    exactly 100.0 across the twelve per-processor rows, so a test that forbade
    the equality outright would be forbidding a real reading.
    """
    perf = perf_rows(*([99.0] * 8 + [102.0] * 4))
    assert sum(float(r.PercentProcessorPerformance) for r in perf) / len(perf) == 100.0
    probe = clock_probe(perf=perf, processors=[SimpleNamespace(MaxClockSpeed=4501)])
    assert probe.read() == {"cpu_nominal_mhz": 4501.0, "cpu_live_mhz": 4501.0}


def test_an_aggregate_at_a_plausible_ratio_is_still_not_a_processor():
    """The rejection is about *which rows* answered, not about the value.

    A turbo-looking 99 on `_Total` is no more a measurement of any individual
    processor than 100 is. If the band were the only guard, this one would slip
    through and look like a live reading.
    """
    probe = clock_probe(
        perf=[SimpleNamespace(Name="_Total", PercentProcessorPerformance=99)],
        processors=[SimpleNamespace(MaxClockSpeed=4501)],
    )
    assert probe.read()["cpu_live_mhz"] is None


@pytest.mark.parametrize(
    "ratio",
    [0, 0.0, -1, -42, float("inf"), float("-inf"), float("nan"),
     0.5, 1.0, 1e-06, 9.9, 201, 255, 4501],
)
def test_a_ratio_outside_the_plausible_band_is_not_a_measurement(ratio):
    # The module's own docstring promises nothing here invents a number, and a
    # counter with no range check does: 0 renders 0.00 GHz, -42 as -1.89 GHz,
    # inf as inf, 255 as 11.48 GHz on a 4.5 GHz part, and a merely tiny positive
    # ratio renders 0.04 GHz. All measured on this code.
    probe = clock_probe(perf=perf_rows(ratio),
                        processors=[SimpleNamespace(MaxClockSpeed=4501)])
    assert probe.read() == {"cpu_nominal_mhz": 4501.0, "cpu_live_mhz": None}, ratio


@pytest.mark.parametrize("ratio", [10.0, 99.0, 100.0, 118.0, 155.0, 200.0])
def test_a_plausible_ratio_is_still_derived(ratio):
    # The band has to leave room for genuine boost, not just for the 99-100 this
    # machine idles at. 155% is below what real silicon reaches: the widest turbo
    # ratio on any shipping part is a ~3.7 GHz Xeon over a 2.4 GHz base, about
    # 154%. A ceiling at 200 clears that with room and still rejects garbage,
    # since nothing clocks a CPU at twice nominal. 10% is the floor for the same
    # reason at the bottom: no x86 core has ever run at a tenth of its nominal
    # clock while an OS is scheduling it, and a formatted counter reports integer
    # percent, so there is nothing real down there to keep.
    probe = clock_probe(perf=perf_rows(ratio),
                        processors=[SimpleNamespace(MaxClockSpeed=4500)])
    assert probe.read()["cpu_live_mhz"] == round(4500 * ratio / 100.0, 1), ratio


@pytest.mark.parametrize("speed", [20001.0, 450100.0, 1e30, 1e300])
def test_a_nominal_faster_than_any_shipped_core_is_dropped(speed):
    # Positive and finite is not enough. The fastest core in production is about
    # 5.7 GHz, so a ceiling at 20 GHz cannot reject a real part, while 450100 --
    # a unit slip in whatever produced the value -- renders "450.10 GHz" as
    # though it were a clock. There is no part to measure, so it is not measured.
    probe = CpuClockProbe(
        wmi=FakeWmi(FakeWmiConnection(processors=[SimpleNamespace(MaxClockSpeed=speed)],
                                      perf=perf_rows(99.0))),
        nominal_max=lambda: None,
    )
    assert probe.read() == {"cpu_nominal_mhz": None, "cpu_live_mhz": None}, speed


@pytest.mark.parametrize("speed", [0, -4501, -0.5, float("nan"), float("inf")])
def test_a_nominal_that_is_not_a_positive_finite_speed_is_dropped(speed):
    # `if speed:` is a truthiness test, and a negative, NaN or infinite ceiling
    # is perfectly truthy. A negative nominal is worse than none at all: it
    # multiplies straight into a negative clock on the panel.
    probe = CpuClockProbe(
        wmi=FakeWmi(FakeWmiConnection(processors=[SimpleNamespace(MaxClockSpeed=speed)],
                                      perf=perf_rows(99.0))),
        nominal_max=lambda: None,
    )
    assert probe.read() == {"cpu_nominal_mhz": None, "cpu_live_mhz": None}, speed


@pytest.mark.parametrize("speed", [0, -4501, float("nan"), float("inf"),
                                  20001.0, 450100.0, 1e300])
def test_a_secondary_nominal_that_is_not_a_positive_finite_speed_is_dropped(speed):
    # The psutil fallback is a source like any other and gets the same checks,
    # bounds included. Trusting one path and not the other is how -4501 gets onto
    # the panel, and how a unit slip in psutil's own field would too: the absurd
    # values are here as well as in the WMI test because the ceiling is a property
    # of the quantity, not of where it was read from.
    probe = clock_probe(perf=perf_rows(99.0), processors=[], nominal=speed)
    assert probe.read() == {"cpu_nominal_mhz": None, "cpu_live_mhz": None}, speed


def test_the_nominal_clock_is_read_once_for_the_life_of_the_process():
    # 6.9 ms a tick, for a value that cannot change while the process runs: the
    # CPU's ceiling is a property of the installed part, not a reading. The
    # performance counter is still read every tick, so the derived clock keeps
    # moving -- only the reference stops being re-asked.
    connection = FakeWmiConnection(processors=[SimpleNamespace(MaxClockSpeed=4501)],
                                   perf=perf_rows(99.0))
    probe = CpuClockProbe(wmi=FakeWmi(connection), nominal_max=lambda: None)
    values = [probe.read() for _ in range(5)]
    assert all(v["cpu_live_mhz"] == 4456.0 for v in values)
    assert sum("Win32_Processor" in q for q in connection.queries) == 1, connection.queries
    assert sum("PercentProcessorPerformance" in q for q in connection.queries) == 5


def test_the_nominal_clock_is_still_retried_while_it_is_unknown():
    # Caching must not cache a *failure*. A provider that was not ready at
    # startup -- or a WMI service that comes back mid-session -- gets asked
    # again, or the row reads -- for the rest of the session.
    connection = FakeWmiConnection(processors=[], perf=perf_rows(99.0))
    wmi = FakeWmi(connection)
    probe = CpuClockProbe(wmi=wmi, nominal_max=lambda: None)
    assert probe.read() == {"cpu_nominal_mhz": None, "cpu_live_mhz": None}
    connection._processors = [SimpleNamespace(MaxClockSpeed=4501)]
    assert probe.read()["cpu_live_mhz"] == 4456.0


def test_the_cpu_clock_probe_reconnects_once_when_the_cached_handle_goes_stale():
    # A WMI service that restarts mid-session leaves the cached connection
    # unusable and every later query failing. Without a reconnect the nominal
    # falls back to psutil and the derived clock reads -- for the rest of the
    # session, however many ticks go by. A handle that recovers on reconnect
    # costs not even a dash, because the retry happens inside the same read.
    connection = FakeWmiConnection(processors=[SimpleNamespace(MaxClockSpeed=4501)],
                                   perf=perf_rows(99.0))
    wmi = FakeWmi(connection)
    probe = CpuClockProbe(wmi=wmi, nominal_max=lambda: None)
    assert probe.read()["cpu_live_mhz"] == 4456.0

    connection.fail = True                      # the service restarts
    assert probe.read()["cpu_live_mhz"] == 4456.0, (
        "the probe did not recover on the reconnect inside its own read"
    )
    assert wmi.connects == 2


def test_a_dead_wmi_service_is_not_reconnected_to_once_per_query():
    # Three queries go into a read. A service that is genuinely down should be
    # failed fast, not re-connected to three times a tick for nothing: the
    # reconnect is paid once per read, however many of its queries then fail.
    wmi = FakeWmi(fail_connect=True)
    probe = CpuClockProbe(wmi=wmi, nominal_max=lambda: None)
    probe.read()
    first = wmi.connects
    probe.read()
    assert wmi.connects == first + 1, (
        f"connect attempts went {first} -> {wmi.connects} for one read"
    )


def test_a_counter_reset_reseeds_the_average_instead_of_decaying_it():
    # 300 KB/s, then the link dies and the adapter's counters restart. The
    # tick that notices says nothing -- the bytes are unknown, not zero -- and
    # the average is dropped rather than averaged, because an EMA decays from
    # its peak: measured here as 300 -> 195 -> 127 -> 82 -> 54 -> 35 -> 23 KB/s,
    # six ticks of throughput nobody sent after the link had gone. The tick
    # after that seeds from a real measurement instead.
    probe = net_probe([[("Ethernet", 600_000, 600_000)],
                       [("Ethernet", 1_200_000, 1_200_000)],
                       [("Ethernet", 0, 0)],
                       [("Ethernet", 40_000, 40_000)]])
    probe.read()
    probe._now.advance(2.0)
    assert probe.read()["net_down_bytes_per_sec"] == 300_000.0
    probe._now.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}
    probe._now.advance(2.0)
    # 40000 bytes over 2 s, seeded fresh: 20_000, not 0.65 * 300_000 = 195_000.
    assert probe.read()["net_down_bytes_per_sec"] == 20_000.0


def test_a_new_adapter_publishes_its_lifetime_as_this_interval():
    """The defect: a cumulative counter's whole history, read as one delta.

    A VPN adapter appears between two ticks carrying 9 GB of counters accumulated
    since boot. Summed into this interval that is 1,575,001,000 B/s -- DN 1575.0
    MB/s on the panel, and a row in the CSV -- and the EMA then carries it for
    roughly six more ticks as it decays at 0.65 per tick. About 30 seconds of
    throughput nobody sent.
    """
    giga = 1024 ** 3
    probe = net_probe([[("Ethernet", 100_000_000, 50_000_000)],
                       [("Ethernet", 120_000_000, 60_000_000),
                        ("VPN Adapter", 9 * giga, 3 * giga)]])
    probe.read()
    probe._now.advance(2.0)
    values = probe.read()
    assert values == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}, values


def test_an_adapter_appearing_on_a_non_advancing_tick_still_drops_the_average():
    """The third path to the same artefact.

    The churn comparison sat *below* the `elapsed <= 0` guard, so an adapter that
    arrived on a tick where the wall clock had not advanced was never noticed as
    a churn at all: the tick returned as an ordinary unmeasurable one, the
    average survived it, and the next tick read 202000 against a true 20000 --
    byte for byte the artefact wave 3 removed, reached through the branch that
    was supposed to be the guard's partner.

    A clock stepping backwards is not exotic: time.time() is not monotonic, an
    NTP correction does it, and two reads close enough together land on the same
    tick of the system clock.
    """
    giga = 1024 ** 3
    clock = FakeClock(100.0)
    adapters = {"Ethernet": (0, 0)}

    def io(pernic=True):
        return {n: SimpleNamespace(bytes_recv=r, bytes_sent=s)
                for n, (r, s) in adapters.items()}

    probe = NetProbe(io_counters=io, now=clock)
    probe.read()
    clock.advance(2.0)
    adapters["Ethernet"] = (600_000, 600_000)
    assert probe.read() == {"net_down_bytes_per_sec": 300_000.0,
                            "net_up_bytes_per_sec": 300_000.0}

    adapters["Ethernet"] = (620_000, 620_000)
    adapters["VPN Adapter"] = (9 * giga, 3 * giga)
    clock.advance(-1.0)                      # the clock steps back on the churn tick
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}

    clock.advance(2.0)
    adapters["Ethernet"] = (640_000, 640_000)
    adapters["VPN Adapter"] = (9 * giga + 20_000, 3 * giga + 20_000)
    # 40000 bytes over 2 s. Not 202000, which is 0.35*20000 + 0.65*300000.
    assert probe.read() == {"net_down_bytes_per_sec": 20_000.0,
                            "net_up_bytes_per_sec": 20_000.0}


def test_an_adapter_vanishing_on_a_non_advancing_tick_still_reports_no_number():
    # Same ordering bug, worse consequence. The departed adapter's final
    # interval is a term missing from the sum -- the argument the code comment
    # gives for settling on set *equality* -- and skipping the comparison let it
    # be dropped in silence. 390000 here against a true 620000, with no dash on
    # the tick that dropped it.
    clock = FakeClock(100.0)
    adapters = {"Ethernet": (0, 0), "VPN Adapter": (0, 0)}

    def io(pernic=True):
        return {n: SimpleNamespace(bytes_recv=r, bytes_sent=s)
                for n, (r, s) in adapters.items()}

    probe = NetProbe(io_counters=io, now=clock)
    probe.read()
    clock.advance(2.0)
    adapters["Ethernet"] = (600_000, 600_000)
    adapters["VPN Adapter"] = (600_000, 600_000)
    assert probe.read() == {"net_down_bytes_per_sec": 600_000.0,
                            "net_up_bytes_per_sec": 600_000.0}

    adapters["Ethernet"] = (1_240_000, 1_240_000)   # both moved 620000
    del adapters["VPN Adapter"]
    clock.advance(-1.0)
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}

    clock.advance(2.0)
    adapters["Ethernet"] = (1_260_000, 1_260_000)   # a further 20000 on Ethernet alone
    # 20000 bytes over 2 s. 620000 was moved over the interrupted interval and
    # 600000 of it is now unreachable, so 390000 of it went missing in silence.
    assert probe.read()["net_down_bytes_per_sec"] == 10_000.0, (
        "the vanished adapter's final interval was dropped from the sum without "
        "the tick saying so"
    )


def test_a_non_advancing_tick_with_no_churn_keeps_the_average():
    """The over-correction guard, and it is the same arithmetic as the two above.

    Nothing discontinuous happened on this tick, so the average is still a valid
    description of the link and must survive: the next measured tick blends with
    it, 0.35*20000 + 0.65*300000 = 202000. That is the *correct* figure here and
    the artefact on the churn tick, and the only thing that distinguishes them is
    whether the adapter set changed -- so hoisting the comparison must not have
    swept the elapsed guard's own case up with it.
    """
    clock = FakeClock(100.0)
    adapters = {"Ethernet": (0, 0)}

    def io(pernic=True):
        return {n: SimpleNamespace(bytes_recv=r, bytes_sent=s)
                for n, (r, s) in adapters.items()}

    probe = NetProbe(io_counters=io, now=clock)
    probe.read()
    clock.advance(2.0)
    adapters["Ethernet"] = (600_000, 600_000)
    assert probe.read()["net_down_bytes_per_sec"] == 300_000.0

    clock.advance(-1.0)                      # no churn, only a backwards clock
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}

    clock.advance(2.0)
    adapters["Ethernet"] = (640_000, 640_000)
    assert probe.read()["net_down_bytes_per_sec"] == 202_000.0, (
        "the average was dropped on a tick where nothing discontinuous happened"
    )


def test_adapter_churn_drops_the_average_built_before_it():
    # A 300 KB/s average is established, then a VPN adapter appears. The churn
    # tick is unmeasured, but the *next* tick's true traffic is 20 KB/s and the
    # average has to go with it. Carried across the churn, the EMA blends the
    # real 20000 into the dead 300000 at 0.35/0.65 and reads 202000 on the first
    # recovery tick -- then 138300, 96895, 69982, 52488 as it decays: the same
    # six-tick artefact the counter-reset path was fixed for, reached through the
    # branch whose comment claims the two paths agree.
    giga = 1024 ** 3
    probe = net_probe([
        [("Ethernet", 0, 0)],
        [("Ethernet", 600_000, 600_000)],
        [("Ethernet", 620_000, 620_000), ("VPN Adapter", 9 * giga, 3 * giga)],
        [("Ethernet", 640_000, 640_000), ("VPN Adapter", 9 * giga + 20_000, 3 * giga + 20_000)],
        [("Ethernet", 660_000, 660_000), ("VPN Adapter", 9 * giga + 40_000, 3 * giga + 40_000)],
    ])
    probe.read()
    probe._now.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": 300_000.0,
                            "net_up_bytes_per_sec": 300_000.0}, (
        "the fixture never established the average this test is about"
    )
    probe._now.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}
    probe._now.advance(2.0)
    # 40000 bytes over 2 s -- the real figure, not a blend with the old peak.
    # Both directions, because the average is held per key and clearing only one
    # of them would leave the other decaying.
    assert probe.read() == {"net_down_bytes_per_sec": 20_000.0,
                            "net_up_bytes_per_sec": 20_000.0}
    probe._now.advance(2.0)
    # And it stays there: a fresh seed, so no decay tail behind it either.
    assert probe.read() == {"net_down_bytes_per_sec": 20_000.0,
                            "net_up_bytes_per_sec": 20_000.0}


def test_a_vanishing_adapter_drops_the_average_too():
    # Same argument on the other side of the set comparison, and the reason the
    # check is set *equality* rather than one-directional: the departed adapter's
    # final interval is a term missing from the sum, so the total is as
    # unknowable as on the appearing side.
    probe = net_probe([
        [("Ethernet", 0, 0), ("VPN Adapter", 0, 0)],
        [("Ethernet", 600_000, 600_000), ("VPN Adapter", 600_000, 600_000)],
        [("Ethernet", 620_000, 620_000)],
        [("Ethernet", 640_000, 640_000)],
    ])
    probe.read()
    probe._now.advance(2.0)
    # Both adapters at 600_000 and summed: 1_200_000 over 2 s.
    assert probe.read() == {"net_down_bytes_per_sec": 600_000.0,
                            "net_up_bytes_per_sec": 600_000.0}, (
        "the fixture never established the average this test is about"
    )
    probe._now.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}
    probe._now.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": 10_000.0,
                            "net_up_bytes_per_sec": 10_000.0}


def test_an_adapter_that_appears_and_stays_measures_normally_from_the_next_tick():
    # The recovery is the half that matters: a policy that simply gave up on the
    # network would pass the test above and break the panel permanently. Both
    # adapters are in both samples by the third tick, so both real deltas count.
    giga = 1024 ** 3
    probe = net_probe([
        [("Ethernet", 0, 0)],
        [("Ethernet", 20_000, 10_000), ("VPN Adapter", 9 * giga, 3 * giga)],
        [("Ethernet", 40_000, 20_000), ("VPN Adapter", 9 * giga + 30_000, 3 * giga + 20_000)],
    ])
    probe.read()
    probe._now.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}
    probe._now.advance(2.0)
    # Ethernet moved 20 kB, the VPN 30 kB: 50 kB over 2 s, not 9 GB.
    assert probe.read() == {"net_down_bytes_per_sec": 25_000.0,
                            "net_up_bytes_per_sec": 15_000.0}


def test_an_adapter_disappearing_also_makes_the_tick_unmeasured():
    # Set equality rather than a one-directional "anything new appeared" check.
    # A vanished adapter's last interval is unaccounted for, so the sum is not
    # the machine's traffic either; and one condition a reader can check at a
    # glance is worth more than saving a tick that costs one dash.
    probe = net_probe([[("Ethernet", 0, 0), ("VPN Adapter", 0, 0)],
                       [("Ethernet", 20_000, 10_000)]])
    probe.read()
    probe._now.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}


def test_a_pseudo_adapter_appearing_does_not_cost_the_tick():
    # The exclusion happens before the names are compared, so a loopback or
    # pseudo interface coming and going -- which Windows does on its own -- must
    # not blank a rate that was perfectly measurable throughout.
    probe = net_probe([
        [("Ethernet", 0, 0)],
        [("Ethernet", 20_000, 10_000), ("Loopback Pseudo-Interface 1", 5_000_000, 5_000_000)],
        [("Ethernet", 40_000, 20_000), ("Loopback Pseudo-Interface 1", 9_000_000, 9_000_000)],
    ])
    probe.read()
    probe._now.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": 10_000.0,
                            "net_up_bytes_per_sec": 5_000.0}


def test_a_repository_that_rejects_a_projection_is_not_reconnected_to_every_tick():
    # A permanently invalid projection is not a stale handle. Answering both with
    # "reconnect" made "one connection per process" untrue in exactly the degraded
    # case: measured at 1.3 connects a tick over three reads, ~2.3 ms each.
    connection = FakeWmiConnection(processors=[], perf=[],
                                   absent=("MaxClockSpeed", "ProcessorFrequency",
                                           "PercentProcessorPerformance"))
    wmi = FakeWmi(connection)
    probe = CpuClockProbe(wmi=wmi, nominal_max=lambda: None)
    for _ in range(3):
        assert probe.read() == {"cpu_nominal_mhz": None, "cpu_live_mhz": None}
    settled = wmi.connects
    for _ in range(20):
        probe.read()
    assert wmi.connects == settled, (
        f"connect attempts went {settled} -> {wmi.connects} over 20 reads of a "
        "projection the repository rejects outright"
    )


def test_a_repository_that_rejects_a_projection_still_takes_a_fresh_connection():
    # Marking a query rejected must not stop the probe recovering from a stale
    # handle later, which is the opposite failure: the clock would go dashed for
    # the rest of the session after one transient outage.
    connection = FakeWmiConnection(processors=[SimpleNamespace(MaxClockSpeed=4501)],
                                   perf=perf_rows(99.0))
    wmi = FakeWmi(connection)
    probe = CpuClockProbe(wmi=wmi, nominal_max=lambda: None)
    assert probe.read()["cpu_live_mhz"] == 4456.0

    connection.fail = True          # the service restarts
    wmi.down = True
    assert probe.read()["cpu_live_mhz"] is None

    connection.fail = False         # and returns; nothing was marked rejected
    wmi.down = False
    assert probe.read()["cpu_live_mhz"] == 4456.0
    assert wmi.connects == 3


def test_real_net_probe_reads_this_machine():
    # No assertion on the value: an idle machine legitimately transfers nothing.
    # What is asserted is that the plumbing works against the real driver, and
    # that the very first sample is unmeasured rather than a fabricated zero.
    probe = NetProbe()
    assert probe.read() == {"net_down_bytes_per_sec": None, "net_up_bytes_per_sec": None}
    values = probe.read()
    assert set(values) == set(metrics.NET_KEYS)
    assert all(value is None or value >= 0.0 for value in values.values())


def test_real_probe_runs_on_this_machine():
    gpu = GpuProbe()
    probe = SystemProbe(gpu=gpu)
    snap = probe.sample()
    assert snap.ram_total_gb and snap.ram_total_gb > 0
    # Where NVML works, the GPU numbers must be real ones -- this test used to
    # pass with a completely broken GPU path because it only looked at RAM.
    # On a machine without an NVIDIA card there is nothing to assert, and the
    # test must not become a hardware requirement, so it stays tolerant.
    if gpu.available:
        assert snap.gpu_pct is not None
        assert snap.gpu_temp_c is not None
        # VRAM in use is a real number, but it is legitimately 0 on an idle
        # card with no display attached, so it must not be asserted non-zero.
        assert snap.vram_used_gb is not None
        assert snap.vram_total_gb and snap.vram_total_gb > 0


def test_history_log_stays_off_when_the_file_cannot_be_opened(tmp_path):
    from history import HistoryLog

    # A regular file where a directory needs to be: mkdir cannot succeed,
    # so this exercises the failure branch rather than the happy path.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    log = HistoryLog(blocker / "sub" / "history.log")
    assert log.enable() is False
    assert log.enabled is False


def test_history_log_writes_a_header_once(tmp_path):
    from history import HistoryLog

    path = tmp_path / "history.log"
    log = HistoryLog(path)
    assert log.enable() is True
    log.write(Snapshot(cpu_pct=10.0, ts=1.0))

    log.disable()
    assert log.enabled is False
    log.write(Snapshot(cpu_pct=20.0, ts=2.0))

    log.enable()
    log.write(Snapshot(cpu_pct=30.0, ts=3.0))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    # Header plus the two writes that happened while the log was enabled.
    assert len(lines) == 3
    assert lines[0].split(",")[0] == "ts"
    # Built from the column count rather than written out, so adding a column
    # does not turn this into a test that has to be edited to keep passing.
    from history import _csv

    # Cells through the writer's own formatter, tail from the column count, so
    # neither has to be edited when a metric joins the dataclass.
    blank = ",".join([""] * (len(Snapshot.CSV_COLUMNS) - 2))
    assert lines[1] == f"{_csv(1.0)},{_csv(10.0)},{blank}"
    assert lines[2] == f"{_csv(3.0)},{_csv(30.0)},{blank}"


def test_history_log_header_matches_the_row_width(tmp_path):
    from history import HistoryLog

    path = tmp_path / "history.log"
    log = HistoryLog(path)
    log.enable()
    log.write(Snapshot(cpu_pct=1.0, ts=2.0))
    header, row = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(header.split(",")) == len(row.split(","))


def test_snapshot_always_defines_gpu_temp_c():
    # theme.metric_state() and theme.has_any_data() read this field. They read
    # it with getattr, so dropping the field would not raise -- the GPU
    # temperature would just silently vanish from the panel instead.
    assert "gpu_temp_c" in {f.name for f in fields(Snapshot)}
    assert Snapshot().gpu_temp_c is None


def test_as_dict_keys_follow_csv_columns_with_ts_first():
    # HistoryLog derives the CSV header from as_dict()'s key order, so the
    # order is part of the file format. dataclasses.asdict() follows field
    # declaration order, which puts ts last: every column of every existing
    # trace shifts and no row changes arity, so nothing catches it downstream.
    assert list(Snapshot().as_dict()) == list(Snapshot.CSV_COLUMNS)
    assert next(iter(Snapshot().as_dict())) == "ts"


def test_csv_columns_covers_every_declared_field():
    # A field added to the dataclass but not to CSV_COLUMNS is dropped from the
    # CSV without changing the arity, so the header/row width check stays
    # green while a real metric goes missing.
    assert set(Snapshot.CSV_COLUMNS) == {f.name for f in fields(Snapshot)}


def test_every_csv_column_holds_the_value_that_names_it(tmp_path):
    # The Task-3 bug, guarded from the other side. asdict() and the writer both
    # follow one dict, so a declared order that disagreed with it would shift
    # every column after the first with no row changing arity -- a width check
    # cannot see it and a header-only check cannot either, because the header is
    # derived from the very same dict. Filling every column with a value unique
    # to it and reading each back by declared index is what notices.
    from history import HistoryLog

    filled = {
        "ts": 7.0, "cpu_pct": 11.0, "cpu_live_mhz": 4464.0, "cpu_nominal_mhz": 4501.0,
        "ram_used_gb": 11.4, "ram_total_gb": 32.0,
        "gpu_pct": 61.0, "gpu_temp_c": 58.0, "vram_used_gb": 6.2, "vram_total_gb": 20.0,
        "net_down_bytes_per_sec": 15000.0, "net_up_bytes_per_sec": 30000.0,
    }
    path = tmp_path / "history.log"
    log = HistoryLog(path)
    log.enable()
    log.write(Snapshot(**filled))

    header, row = path.read_text(encoding="utf-8").strip().splitlines()
    assert header == ",".join(Snapshot.CSV_COLUMNS)
    columns = row.split(",")
    assert len(columns) == len(Snapshot.CSV_COLUMNS)
    for name, value in filled.items():
        index = Snapshot.CSV_COLUMNS.index(name)
        assert columns[index] == repr(value), (
            f"column {index} holds {columns[index]!r} but declares {name!r}: "
            "the row and the header disagree about the order"
        )


def test_snapshot_always_defines_the_frequency_and_network_fields():
    # painter.py reads these straight off the dataclass, so dropping one would
    # raise AttributeError out of paint() rather than degrade to a dash -- and
    # overlay.py swallows that, which costs a frame every two seconds instead
    # of failing loudly. getattr accessors would hide the same deletion.
    names = {f.name for f in fields(Snapshot)}
    assert {"cpu_live_mhz", "cpu_nominal_mhz", *metrics.NET_KEYS} <= names
    empty = Snapshot()
    assert empty.cpu_live_mhz is None
    assert empty.cpu_nominal_mhz is None
    for key in metrics.NET_KEYS:
        assert getattr(empty, key) is None


def test_the_nominal_frequency_no_longer_has_a_second_field():
    # cpu_max_mhz was psutil's ceiling and cpu_nominal_mhz is the same quantity
    # from a better source. Keeping both invites a row that prints one as the
    # live clock, which is the bug being fixed; there is one reference now.
    assert "cpu_mhz" not in {f.name for f in fields(Snapshot)}
    assert "cpu_max_mhz" not in {f.name for f in fields(Snapshot)}


def test_the_network_columns_appear_in_the_declared_direction_order():
    # The panel header prints download before upload and the CSV records them in
    # that order too. Two hand-written orders would drift apart silently.
    assert metrics.NET_KEYS == ("net_down_bytes_per_sec", "net_up_bytes_per_sec")
    columns = list(Snapshot.CSV_COLUMNS)
    assert columns.index("net_down_bytes_per_sec") < columns.index("net_up_bytes_per_sec")




# --- how often a failing source may write to the log -----------------------

# Ten minutes at theme.TICK_MS: the number of ticks a persistently broken
# source used to write one record for.
TEN_MINUTE_TICKS = 300


@pytest.fixture
def fault_log(monkeypatch):
    """A fresh fault throttle over a clock the test owns, and a clean slate.

    The throttle is process-wide on purpose -- a source that failed once must
    not have its interval restarted by the next test -- so every test that
    counts records installs its own.
    """
    clock = FakeClock(1_000.0)
    monkeypatch.setattr(
        metrics, "time", SimpleNamespace(monotonic=clock, time=clock)
    )
    metrics._FAULT_LOG.clear()
    yield clock
    metrics._FAULT_LOG.clear()


def records_for(caplog, source):
    return [r for r in caplog.records if r.name == "widget.metrics" and source in r.getMessage()]


def readable_ram():
    return SimpleNamespace(used=11.4 * GB, total=32 * GB)


def boom():
    raise RuntimeError("sensor bus is on fire")


# Each source is driven on its own, because a throttle that counted all sources
# together would pass every one of these while a per-source throttle was missing
# from four of the five call sites.
FAILING_SOURCES = {
    "cpu_percent": lambda: SystemProbe(
        cpu_pct=boom, ram=readable_ram, cpu_clock=FakeCpuClock({}),
        gpu=FakeGpu({}), net=FakeNet(),
    ),
    "virtual_memory": lambda: SystemProbe(
        cpu_pct=lambda: 1.0, ram=boom, cpu_clock=FakeCpuClock({}),
        gpu=FakeGpu({}), net=FakeNet(),
    ),
    "cpu clock read": lambda: SystemProbe(
        cpu_pct=lambda: 1.0, ram=readable_ram, cpu_clock=BrokenCpuClock(),
        gpu=FakeGpu({}), net=FakeNet(),
    ),
    "gpu read": lambda: SystemProbe(
        cpu_pct=lambda: 1.0, ram=readable_ram, cpu_clock=FakeCpuClock({}),
        gpu=BrokenGpu(), net=FakeNet(),
    ),
    "net read": lambda: SystemProbe(
        cpu_pct=lambda: 1.0, ram=readable_ram, cpu_clock=FakeCpuClock({}),
        gpu=FakeGpu({}), net=BrokenNet(),
    ),
}


class FakeNet:
    def read(self):
        return {}


class BrokenNet:
    def read(self):
        raise RuntimeError("sensor bus is on fire")


@pytest.mark.parametrize("source", sorted(FAILING_SOURCES))
def test_a_persistently_failing_source_is_logged_once_per_interval(
    source, caplog, fault_log
):
    """The spec's rule, and the reason app_debug.log had a size problem.

    'Логирование ограничено по частоте: не чаще одной записи на источник в
    минуту' -- at most one record per source per minute. Nothing implemented
    it: every one of these guards called logger.warning(exc_info=True) on
    every tick, so one broken source wrote 300 records -- and 99 KB -- in the
    ten minutes the spec measures the log over.
    """
    probe = FAILING_SOURCES[source]()
    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        for _ in range(TEN_MINUTE_TICKS):
            probe.sample()

    assert len(records_for(caplog, source)) == 1, (
        f"{len(records_for(caplog, source))} records for one source in "
        f"{TEN_MINUTE_TICKS} ticks: the interval is not being enforced"
    )


@pytest.mark.parametrize("source", sorted(FAILING_SOURCES))
def test_a_record_appears_again_once_the_interval_expires(source, caplog, fault_log):
    """A floor, not a latch: the interval has to expire for the next record.

    The previous version kept a memo that a clean frame emptied, so a source
    failing on every other tick looked healthy half the time and still wrote
    one record per bad tick. Silence in between changes nothing.
    """
    probe = FAILING_SOURCES[source]()
    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        for _ in range(TEN_MINUTE_TICKS):
            probe.sample()
        fault_log.advance(metrics.LOG_INTERVAL)
        probe.sample()
        fault_log.advance(metrics.LOG_INTERVAL - 0.001)
        probe.sample()
        fault_log.advance(metrics.LOG_INTERVAL)
        probe.sample()

    assert len(records_for(caplog, source)) == 3, (
        f"{len(records_for(caplog, source))} records across three intervals: "
        "the interval is either not enforced or never reopens"
    )


def test_one_source_going_bad_does_not_silence_another(caplog, fault_log):
    """Per source, not per logger: five broken sources are five floors."""
    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        for _ in range(3):
            SystemProbe(
                cpu_pct=boom, ram=boom, cpu_clock=BrokenCpuClock(),
                gpu=BrokenGpu(), net=BrokenNet(),
            ).sample()

    for source in FAILING_SOURCES:
        assert len(records_for(caplog, source)) == 1, (
            f"{source!r} wrote {len(records_for(caplog, source))} records: "
            "one source's record is suppressing another's"
        )


def test_the_traceback_is_written_once_and_the_summary_after_it(caplog, fault_log):
    """Where the bytes go, since a traceback is most of a record.

    Measured over 300 ticks at the real 2 s cadence, one source broken: 99,300
    bytes before, 1,331 after. Of those 1,331, the traceback is 338 and the ten
    one-line summaries are about 99 each. So the traceback is kept for the
    first record of a source and the exception is carried on the line after it:
    300 dumps become one dump plus ten lines.

    The traceback is not dropped, only rationed: it says *where* the fault is,
    which is what a reader needs and what the summary cannot carry. The
    exception type and message are what can change between two occurrences of
    the same source, so those are on every record.
    """
    probe = FAILING_SOURCES["virtual_memory"]()
    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        for _ in range(10):
            probe.sample()
        fault_log.advance(metrics.LOG_INTERVAL)
        probe.sample()

    written = records_for(caplog, "virtual_memory")
    assert len(written) == 2
    assert written[0].exc_info is not None, (
        "the first record of a fault carries no traceback: there is nothing "
        "left to diagnose it with if the log is all that survives"
    )
    assert written[1].exc_info is None, (
        "the tenth record of the same fault repeated the whole traceback"
    )
    assert "RuntimeError" in written[1].getMessage() and "on fire" in written[1].getMessage(), (
        f"the summary lost the exception it was supposed to carry: "
        f"{written[1].getMessage()!r}"
    )


def test_a_differing_fault_does_not_restart_the_interval(caplog, fault_log):
    """The decision, and the reason for it: time only, never the message.

    Any rule that lets a *changed* fault reopen the floor is defeated by a
    fault whose text varies per occurrence -- two adapters alternating, an
    HRESULT that flips, a counter value in the message -- and then the bound
    is no longer a bound. So the timer runs from the last record written for
    that source, whatever happened in between.

    The cost is stated rather than discovered later: a genuinely new fault is
    not announced until the interval expires, at most 60 s late, and the panel
    is already showing dashes for it.
    """
    alternating = {"n": 0}

    def flip():
        alternating["n"] += 1
        raise RuntimeError(f"attempt {alternating['n']}")

    probe = SystemProbe(
        cpu_pct=flip, ram=readable_ram, cpu_clock=FakeCpuClock({}),
        gpu=FakeGpu({}), net=FakeNet(),
    )
    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        for _ in range(50):
            probe.sample()

    assert len(records_for(caplog, "cpu_percent")) == 1, (
        f"{len(records_for(caplog, 'cpu_percent'))} records from a fault whose "
        "message changes every tick: the interval is keyed on the message"
    )


def test_the_network_counter_failure_is_rate_limited_too(caplog, fault_log):
    """NetProbe logs its own failure, one source away from SystemProbe's.

    SystemProbe catches whatever NetProbe.read() lets out, but read() catches
    its own counter failure and returns two dashes, so this is the record a
    machine with a dead psutil net counter actually produces -- and it was one
    per tick.
    """
    probe = NetProbe(io_counters=boom)
    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        for _ in range(TEN_MINUTE_TICKS):
            probe.read()

    assert len(records_for(caplog, "net_io_counters")) == 1, (
        f"{len(records_for(caplog, 'net_io_counters'))} records in "
        f"{TEN_MINUTE_TICKS} ticks"
    )


def test_the_psutil_nominal_failure_is_rate_limited_too(caplog, fault_log):
    """The clock's own fallback, which fires on every tick until it works."""
    probe = CpuClockProbe(
        wmi=FakeWmi(connection=FakeWmiConnection(processors=(), perf=())),
        nominal_max=boom,
    )
    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        for _ in range(TEN_MINUTE_TICKS):
            probe.read()

    assert len(records_for(caplog, "nominal CPU frequency")) == 1, (
        f"{len(records_for(caplog, 'nominal CPU frequency'))} records in "
        f"{TEN_MINUTE_TICKS} ticks"
    )


def test_the_interval_is_the_one_the_spec_promises():
    # "не чаще одной записи на источник в минуту" -- a minute, not a tick.
    assert metrics.LOG_INTERVAL == 60.0


def test_a_recovered_source_is_not_logged_again_on_its_next_failure(caplog, fault_log):
    """Silence does not hand a source a fresh interval -- it costs a whole one.

    The obvious way to keep a repeated fault quiet is to forget the timer when
    the source recovers, and that turns any flapping source back into one
    record per tick: fail, recover, fail, recover. Nothing here resets.
    """
    failing = {"on": True}

    def flaky():
        if failing["on"]:
            raise RuntimeError("sensor bus is on fire")
        return 1.0

    probe = SystemProbe(
        cpu_pct=flaky, ram=readable_ram, cpu_clock=FakeCpuClock({}),
        gpu=FakeGpu({}), net=FakeNet(),
    )
    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        for index in range(50):
            failing["on"] = index % 2 == 0
            probe.sample()

    assert len(records_for(caplog, "cpu_percent")) == 1, (
        f"{len(records_for(caplog, 'cpu_percent'))} records from a source that "
        "fails on every other tick"
    )


# --- the WMI fallback on a machine with no NVIDIA --------------------------


class CountingFallback:
    """Stands in for wmi_fallback() and counts how often it is asked."""

    def __init__(self, answer=None):
        self.calls = 0
        self.answer = answer if answer is not None else {"vram_total_gb": 8.0}

    def __call__(self):
        self.calls += 1
        return dict(self.answer)


class FallbackClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def no_nvidia_probe(now):
    return GpuProbe(nvml=FakeNvml(fail_init=True), now=now)


def test_the_wmi_fallback_is_not_queried_every_tick(monkeypatch):
    """The spec's acceptance criterion 5 machine: no NVIDIA, and no spam.

    With no NVML handle, read() fell through to wmi_fallback(), which opens a
    *fresh* COM connection and runs Win32_VideoController() -- every tick, every
    two seconds, for a number that is a property of the installed card. So the
    machines the spec singles out as the ones that must work were also the ones
    paying for a COM connect and a WMI query 30 times a minute.

    Cached and re-checked on a slow interval instead of never, because the
    answer can change: a WMI service that is down at startup leaves an empty
    answer cached, and a driver install partway through a session would
    otherwise never be noticed. Sixty seconds is far below anything a person
    watching a panel would see and 30x less WMI.
    """
    clock = FallbackClock()
    fallback = CountingFallback()
    probe = no_nvidia_probe(clock)
    monkeypatch.setattr(metrics, "wmi_fallback", fallback)

    for _ in range(300):
        probe.read()

    assert fallback.calls == 1, (
        f"the WMI fallback was queried {fallback.calls} times in 300 ticks: "
        "a fresh COM connection and a Win32_VideoController query every two "
        "seconds, on exactly the machines without an NVIDIA card"
    )


def test_the_fallback_is_asked_again_after_the_refresh_interval(monkeypatch):
    clock = FallbackClock()
    fallback = CountingFallback()
    probe = no_nvidia_probe(clock)
    monkeypatch.setattr(metrics, "wmi_fallback", fallback)

    probe.read()
    clock.advance(metrics.WMI_FALLBACK_REFRESH_S - 0.001)
    probe.read()
    assert fallback.calls == 1, "the answer was re-queried before the interval"

    clock.advance(0.002)
    probe.read()
    assert fallback.calls == 2, (
        "the cached answer was never refreshed, so a WMI service that was down "
        "at startup leaves the GPU row dashed for the rest of the session"
    )


def test_a_refreshed_fallback_picks_up_a_changed_answer(monkeypatch):
    """The reason for the refresh rather than caching for the process's life."""
    clock = FallbackClock()
    fallback = CountingFallback({"vram_total_gb": 8.0})
    probe = no_nvidia_probe(clock)
    monkeypatch.setattr(metrics, "wmi_fallback", fallback)

    assert probe.read()["vram_total_gb"] == 8.0
    fallback.answer = {"vram_total_gb": 12.0}
    assert probe.read()["vram_total_gb"] == 8.0, (
        "the cache was consulted before the interval, so it is not a cache"
    )

    clock.advance(metrics.WMI_FALLBACK_REFRESH_S)
    assert probe.read()["vram_total_gb"] == 12.0


def test_a_caller_cannot_corrupt_the_cached_answer_by_editing_it(monkeypatch):
    """A copy per read, because SystemProbe reads the dict it is handed."""
    clock = FallbackClock()
    fallback = CountingFallback({"vram_total_gb": 8.0})
    probe = no_nvidia_probe(clock)
    monkeypatch.setattr(metrics, "wmi_fallback", fallback)

    first = probe.read()
    first["vram_total_gb"] = 999.0
    first["gpu_pct"] = 50.0
    assert probe.read()["vram_total_gb"] == 8.0
    assert "gpu_pct" not in probe.read()


def test_an_nvidia_gpu_never_touches_the_wmi_fallback(monkeypatch):
    """The fallback is a fallback. A machine with a card must not pay for it."""
    fallback = CountingFallback()
    probe = GpuProbe(nvml=FakeNvml(), now=FallbackClock())
    monkeypatch.setattr(metrics, "wmi_fallback", fallback)

    for _ in range(300):
        probe.read()

    assert fallback.calls == 0, (
        f"the WMI fallback ran {fallback.calls} times on a machine with a "
        "working NVML handle"
    )


def test_the_refresh_interval_is_longer_than_a_tick():
    assert metrics.WMI_FALLBACK_REFRESH_S == 60.0
    assert metrics.WMI_FALLBACK_REFRESH_S > metrics.LOG_INTERVAL / 1000.0
