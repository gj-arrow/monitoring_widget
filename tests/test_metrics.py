import logging
from dataclasses import fields
from types import SimpleNamespace

import metrics
from metrics import GpuProbe, Snapshot, SystemProbe, wmi_fallback

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

    def __init__(self, fail_init=False, fail_name=False):
        self.calls = {}
        self._fail_init = fail_init
        self._fail_name = fail_name

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
        return FakeUtil(42)

    def nvmlDeviceGetTemperature(self, handle, sensor):
        self._count("temp")
        return 61

    def nvmlDeviceGetMemoryInfo(self, handle):
        self._count("mem")
        return FakeMemory(2 * GB, 8 * GB)


class FakeGpu:
    def __init__(self, values):
        self.values = values

    def read(self):
        return dict(self.values)


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
    controller = SimpleNamespace(AdapterRAM=8 * GB, CurrentClockFrequency=900, MaxClockSpeed=1800)
    monkeypatch.setattr(metrics, "_wmi_video_controllers", lambda: [controller])
    result = wmi_fallback()
    assert result["gpu_pct"] is None
    assert result["gpu_temp_c"] is None
    assert result["vram_used_gb"] is None
    assert result["vram_total_gb"] == 8.0


def test_wmi_fallback_handles_no_controllers(monkeypatch):
    monkeypatch.setattr(metrics, "_wmi_video_controllers", lambda: [])
    assert wmi_fallback()["vram_total_gb"] is None


def test_sample_fills_every_field():
    probe = SystemProbe(
        cpu_pct=lambda: 12.5,
        cpu_freq=lambda: SimpleNamespace(current=3600.0, max=4500.0),
        ram=lambda: SimpleNamespace(used=11.4 * GB, total=32 * GB),
        gpu=FakeGpu({"gpu_pct": 61.0, "gpu_temp_c": 58.0, "vram_used_gb": 6.2, "vram_total_gb": 20.0}),
    )
    snap = probe.sample()
    assert snap.cpu_pct == 12.5
    assert snap.cpu_mhz == 3600.0
    assert snap.cpu_max_mhz == 4500.0
    assert snap.ram_used_gb == 11.4
    assert snap.ram_total_gb == 32.0
    assert snap.gpu_pct == 61.0
    assert snap.gpu_temp_c == 58.0
    assert snap.vram_used_gb == 6.2
    assert snap.vram_total_gb == 20.0
    assert snap.ts > 0.0


def test_sample_drops_a_zero_maximum_frequency():
    probe = SystemProbe(
        cpu_pct=lambda: 1.0,
        cpu_freq=lambda: SimpleNamespace(current=3600.0, max=0.0),
        ram=lambda: SimpleNamespace(used=0, total=0),
        gpu=FakeGpu({}),
    )
    snap = probe.sample()
    assert snap.cpu_mhz == 3600.0
    assert snap.cpu_max_mhz is None


def test_sample_reports_none_when_a_source_raises():
    def boom():
        raise RuntimeError("sensor bus is on fire")

    probe = SystemProbe(
        cpu_pct=boom, cpu_freq=boom, ram=boom, gpu=FakeGpu({})
    )
    snap = probe.sample()
    assert snap.cpu_pct is None
    assert snap.cpu_mhz is None
    assert snap.ram_used_gb is None
    assert snap.ram_total_gb is None
    assert snap.gpu_pct is None
    assert snap.ts > 0.0


def test_snapshot_as_dict_round_trips():
    snap = Snapshot(cpu_pct=5.0)
    assert snap.as_dict()["cpu_pct"] == 5.0
    assert snap.as_dict()["gpu_temp_c"] is None


def test_real_probe_runs_on_this_machine():
    probe = SystemProbe()
    snap = probe.sample()
    assert snap.ram_total_gb and snap.ram_total_gb > 0


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
    assert lines[1] == "1,10,,,,,,,," 
    assert lines[2] == "3,30,,,,,,,,"


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


