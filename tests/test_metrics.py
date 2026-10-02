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

    def WMI(self):
        self.connects += 1
        if self._fail_connect:
            raise RuntimeError("the WMI service has stopped")
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


def test_a_counter_that_went_backwards_reads_as_quiet_not_negative():
    # Adapters are re-enumerated and counters restart. The bytes did not
    # un-transfer, so a negative rate is a nonsense number on screen; zero is
    # the conservative reading of an unknown amount.
    probe = net_probe([[("Ethernet", 900_000, 900_000)],
                       [("Ethernet", 0, 0)]])
    probe.read()
    probe._now.advance(2.0)
    assert probe.read() == {"net_down_bytes_per_sec": 0.0, "net_up_bytes_per_sec": 0.0}


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
    [0, 0.0, -1, -42, float("inf"), float("-inf"), float("nan"), 201, 255, 4501],
)
def test_a_ratio_outside_the_plausible_band_is_not_a_measurement(ratio):
    # The module's own docstring promises nothing here invents a number, and a
    # counter with no range check does: 0 renders 0.00 GHz, -42 renders
    # -1.89 GHz, inf renders inf, and 255 renders 11.48 GHz on a 4.5 GHz part.
    # All four were measured on this code before the band existed.
    probe = clock_probe(perf=perf_rows(ratio),
                        processors=[SimpleNamespace(MaxClockSpeed=4501)])
    assert probe.read() == {"cpu_nominal_mhz": 4501.0, "cpu_live_mhz": None}, ratio


@pytest.mark.parametrize("ratio", [1.0, 99.0, 100.0, 118.0, 155.0, 200.0])
def test_a_plausible_ratio_is_still_derived(ratio):
    # The band has to leave room for genuine boost, not just for the 99-100 this
    # machine idles at. 155% is below what real silicon reaches: the widest turbo
    # ratio on any shipping part is a ~3.7 GHz Xeon over a 2.4 GHz base, about
    # 154%. A ceiling at 200 clears that with room and still rejects garbage,
    # since nothing clocks a CPU at twice nominal.
    probe = clock_probe(perf=perf_rows(ratio),
                        processors=[SimpleNamespace(MaxClockSpeed=4500)])
    assert probe.read()["cpu_live_mhz"] == round(4500 * ratio / 100.0, 1), ratio


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


@pytest.mark.parametrize("speed", [0, -4501, float("nan"), float("inf")])
def test_a_secondary_nominal_that_is_not_a_positive_finite_speed_is_dropped(speed):
    # The psutil fallback is a source like any other and gets the same check.
    # Trusting one path and not the other is how -4501 gets onto the panel.
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
    # session, however many ticks go by.
    connection = FakeWmiConnection(processors=[SimpleNamespace(MaxClockSpeed=4501)],
                                   perf=perf_rows(99.0))
    wmi = FakeWmi(connection)
    probe = CpuClockProbe(wmi=wmi, nominal_max=lambda: None)
    assert probe.read()["cpu_live_mhz"] == 4456.0

    connection.fail = True                      # the service goes away
    assert probe.read()["cpu_live_mhz"] is None, (
        "a stale handle still produced a derived clock"
    )

    replacement = FakeWmiConnection(processors=[SimpleNamespace(MaxClockSpeed=4501)],
                                    perf=perf_rows(99.0))
    connection.fail = False                     # and comes back on a new handle
    wmi._connection = replacement
    assert probe.read()["cpu_live_mhz"] == 4456.0
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
    # 300 KB/s, then the link dies and the adapter's counters restart. An EMA
    # decays from its peak -- measured here as 300 -> 195 -> 127 -> 82 -> 54 ->
    # 35 -> 23 KB/s, six ticks of throughput nobody sent after the link has
    # gone. The counters restarted, so the bytes since the last sample are
    # unknown rather than zero, and the honest thing to do with a dead average
    # is replace it instead of averaging it away.
    probe = net_probe([[("Ethernet", 600_000, 600_000)],
                       [("Ethernet", 1_200_000, 1_200_000)],
                       [("Ethernet", 0, 0)],
                       [("Ethernet", 0, 0)]])
    probe.read()
    probe._now.advance(2.0)
    assert probe.read()["net_down_bytes_per_sec"] == 300_000.0
    probe._now.advance(2.0)
    assert probe.read()["net_down_bytes_per_sec"] == 0.0
    probe._now.advance(2.0)
    assert probe.read()["net_down_bytes_per_sec"] == 0.0


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


