from __future__ import annotations

import copy
import hashlib
import time
from pathlib import Path

import pytest

from tis_tester import rates
from tis_tester import __version__
from tis_tester.aic_ioctl import AicPrivateIoctl
from tis_tester.backends import make_backend
from tis_tester.backends.base import BackendError, RxStats, TestParams as Params
from tis_tester.backends.bt_aic_uart import BtAicUartBackend
from tis_tester.backends.bt_hci import BtHciBackend
from tis_tester.backends.wifi_aic import WifiAicBackend
from tis_tester.config import DEFAULTS
from tis_tester.cli import main
from tis_tester.report import finish_run, record_run_sample, start_run
from tis_tester.report import write_sweep_report
from tis_tester.recovery import _write_selector
from tis_tester import serial_console
from tis_tester.webui import Controller


class FakeIoctl:
    def __init__(self):
        self.calls = []
        self.result = (100, 7)

    def command(self, name, *args):
        self.calls.append((name, args))
        return b""

    def rx_result(self):
        self.calls.append(("get_rx_result", ()))
        return self.result


def config(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["general"]["results_dir"] = str(tmp_path)
    cfg["wifi"]["require_rf_mode"] = False
    return cfg


def test_per_uses_expected_packet_count():
    assert RxStats(packets_ok=900, packets_err=10, expected_packets=1000).per == pytest.approx(.1)
    assert RxStats(packets_ok=90, packets_err=10).per == pytest.approx(.1)


def test_unavailable_crc_count_is_not_reported_as_zero():
    stats = RxStats(packets_ok=25, packets_err=None)
    assert stats.packets_total == 25
    assert stats.per is None


def test_rate_and_channel_validation():
    assert rates.channel_to_freq("2.4GHz", 6) == 2437
    assert rates.channel_to_freq("5GHz", 36) == 5180
    with pytest.raises(ValueError):
        rates.channel_to_freq("5GHz", 6)
    with pytest.raises(ValueError):
        rates.channels_for_band("invalid")


def test_package_versions_are_consistent():
    root = Path(__file__).parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject
    assert f'version = "{__version__}";' in (root / "flake.nix").read_text(encoding="utf-8")


def test_unsafe_private_command_is_blocked_before_ioctl():
    client = AicPrivateIoctl("wlan0")
    with pytest.raises(BackendError, match="Blocked unsafe"):
        client.command("set_freq_cal", 1)


def test_optional_restore_selector_does_not_fail_recovery(tmp_path):
    report = {"actions": [], "errors": []}
    _write_selector(str(tmp_path / "optional-missing"), "0", report, required=False)
    assert not report["errors"]
    _write_selector(str(tmp_path / "required-missing"), "0", report, required=True)
    assert report["errors"]


def test_wifi_ioctl_maps_he_tx_parameters(tmp_path):
    backend = WifiAicBackend(config(tmp_path))
    fake = FakeIoctl()
    backend._ioctl = fake
    p = Params(band="5GHz", channel=36, bandwidth_mhz=40,
               rate="HE-MCS11", tx_power_dbm=17)
    backend.start_tx(p)
    assert fake.calls[0] == ("set_tx", (36, 1, 5, 11, 8192, 1000, 17))
    backend.stop()
    assert fake.calls[-1] == ("set_txstop", ())


def test_wifi_rx_reports_crc_errors_from_total(tmp_path):
    backend = WifiAicBackend(config(tmp_path))
    fake = FakeIoctl()
    backend._ioctl = fake
    p = Params(channel=6, rate="HE-MCS0")
    backend.start_rx(p)
    fake.result = (125, 9)
    stats = backend.poll_rx()
    assert (stats.packets_ok, stats.packets_err) == (25, 2)


def test_bt_hci_command_complete_parser():
    out = "> HCI Event: 0x0e plen 6\n 01 1f 20 00 34 12"
    assert BtHciBackend._parse_event(out) == bytes.fromhex("01 1f 20 00 34 12")


def test_bt_aic_uart_test_end_parser():
    out = "EVENT(9): 04 0E 06 05 1F 20 00 34 12"
    assert BtAicUartBackend._parse_test_end(out) == 0x1234


def test_bt_aic_uart_test_end_parser_tolerates_noise():
    out = "\n".join([
        "[123.4] kernel noise",
        "EVENT(9): 04 0E 06 05 1F 20 00 00 00",
        "extra line",
    ])
    assert BtAicUartBackend._parse_test_end(out) == 0


def test_bt_aic_uart_extracts_last_event():
    out = "\n".join([
        "EVENT(9): 04 0E 06 05 1F 20 00 01 00",
        "EVENT(9): 04 0E 06 05 1F 20 00 34 12",
    ])
    assert BtAicUartBackend._parse_test_end(out) == 0x1234


def test_bt_aic_uart_selected_by_default(tmp_path):
    backend = make_backend("bt", config(tmp_path))
    assert isinstance(backend, BtAicUartBackend)


def test_bt_aic_uart_requires_1m_phy(tmp_path):
    backend = BtAicUartBackend(config(tmp_path))
    with pytest.raises(BackendError, match="validated for: 1M"):
        backend._validate(Params(radio="bt", channel=19, rate="2M"))


def test_bt_aic_uart_accumulates_counts_across_live_polls(tmp_path):
    backend = BtAicUartBackend(config(tmp_path))
    backend.service_log = tmp_path / "bt-service.log"
    backend._ensure_open = lambda: None
    counts = iter((3, 4, 2))

    def fake_run(*args, **kwargs):
        if args[0] == "-c":
            count = next(counts)
            return f"EVENT(9): 04 0E 06 05 1F 20 00 {count & 0xff:02X} {count >> 8:02X}"
        return "status 0x00"

    backend._run_bt_test = fake_run
    params = Params(radio="bt", channel=19, bandwidth_mhz=1, rate="1M",
                    expected_packets=10)
    backend.start_rx(params)
    first = backend.poll_rx()
    second = backend.poll_rx()
    final = backend.stop()
    assert (first.packets_ok, second.packets_ok, final.packets_ok) == (3, 7, 9)
    assert final.packets_err is None
    assert final.per == pytest.approx(0.1)


def test_bt_aic_uart_failed_open_restores_host_stack(tmp_path, monkeypatch):
    backend = BtAicUartBackend(config(tmp_path))
    events = []
    monkeypatch.setattr("tis_tester.backends.bt_aic_uart.os.path.exists", lambda _: True)
    backend._stop_existing_tool = lambda: None
    backend._rfkill = lambda action, target: events.append(("rfkill", action, target))

    def service(action, services):
        events.append(("service", action, tuple(services)))
        if action == "stop":
            raise BackendError("stop failed")

    backend._service_cmd = service
    with pytest.raises(BackendError, match="stop failed"):
        backend.open()
    assert ("rfkill", "unblock", "bluetooth") in events
    assert any(event[:2] == ("service", "start") for event in events)


def test_web_tx_requires_arm_and_auto_expires(tmp_path):
    cfg = config(tmp_path)
    cfg["general"]["max_tx_duration_s"] = 0.1
    ctrl = Controller(cfg, force_mock=True)
    with pytest.raises(BackendError, match="not armed"):
        ctrl.start_tx()
    ctrl.arm_tx()
    ctrl.start_tx()
    assert ctrl.mode == "tx"
    time.sleep(0.25)
    assert ctrl.mode is None
    ctrl.close()


def test_web_bt_selection_normalizes_bandwidth_and_hides_unvalidated_phys(tmp_path):
    ctrl = Controller(config(tmp_path))
    ctrl.set_params({"radio": "bt"})
    state = ctrl.state()
    assert state["params"]["bandwidth_mhz"] == 1
    assert state["options"]["rate_groups"]["PHY"] == ["1M"]


def test_web_failed_radio_start_discards_prepared_run(tmp_path):
    class FailingBackend:
        _radio = "wifi"
        mode = None

        def start_rx(self, params):
            raise BackendError("start failed")

        def stop(self):
            return None

        def close(self):
            pass

    ctrl = Controller(config(tmp_path))
    ctrl.backend = FailingBackend()
    with pytest.raises(BackendError, match="start failed"):
        ctrl.start_rx()
    assert ctrl.mode is None
    assert ctrl.logger is None
    assert ctrl._active_run is None


def test_web_failed_stop_closes_backend_and_finishes_error_report(tmp_path):
    class FailingStopBackend:
        _radio = "wifi"
        mode = "rx"
        closed = False

        def stop(self):
            raise BackendError("test end failed")

        def close(self):
            self.closed = True

    ctrl = Controller(config(tmp_path))
    backend = FailingStopBackend()
    ctrl.backend = backend
    ctrl.mode = "rx"
    ctrl._begin_run("rx")
    with pytest.raises(BackendError, match="test end failed"):
        ctrl.stop()
    assert backend.closed
    assert ctrl.mode is None
    assert ctrl.backend is None
    assert ctrl.last_report_path and Path(ctrl.last_report_path).is_file()


def test_web_generates_report_after_rx_run(tmp_path):
    cfg = config(tmp_path)
    cfg["general"]["poll_interval_s"] = 0.05
    ctrl = Controller(cfg, force_mock=True)
    ctrl.start_rx()
    time.sleep(0.16)
    ctrl.stop()
    st = ctrl.state()
    report = st["report"]
    assert report is not None
    path = Path(report["path"])
    assert path.is_file()
    html = path.read_text(encoding="utf-8")
    assert "TIS Test Report" in html
    assert "HE-MCS0" in html
    assert "Recent Samples" in html
    assert st["log"] and st["log"].endswith(".csv")
    ctrl.close()


def test_report_helpers_generate_serial_run_report(tmp_path):
    params = Params(radio="bt", band="2.4GHz", channel=19, rate="1M")
    run = start_run(params, "rx", "tis-test serial dashboard",
                    str(tmp_path / "bt_rx.csv"))
    record_run_sample(run, RxStats(packets_ok=95, packets_err=5, expected_packets=100))
    final = RxStats(packets_ok=97, packets_err=5, expected_packets=100)
    path = finish_run(run, tmp_path, "RX stopped", final)
    assert path.is_file()
    html = path.read_text(encoding="utf-8")
    assert "tis-test serial dashboard" in html
    assert "BT" in html
    assert "RX stopped" in html


def test_write_sweep_report(tmp_path):
    path = write_sweep_report(
        tmp_path,
        "tis-test bt-scan",
        "Bluetooth Scan Report",
        [
            {"channel": 19, "freq_mhz": 2440, "rate": "1M", "rssi_dbm": None,
             "per_pct": None, "packets_ok": 0, "packets_err": 0, "packets_total": 0},
            {"channel": 20, "freq_mhz": 2442, "rate": "2M", "rssi_dbm": -70.0,
             "per_pct": 1.5, "packets_ok": 985, "packets_err": 15, "packets_total": 1000},
        ],
        str(tmp_path / "scan.csv"),
    )
    html = path.read_text(encoding="utf-8")
    assert "Bluetooth Scan Report" in html
    assert "2442" in html
    assert "1000" in html


def test_sweep_report_ranks_zero_per_ahead_of_nonzero_per(tmp_path):
    path = write_sweep_report(
        tmp_path, "test", "Rank Test",
        [
            {"channel": 1, "freq_mhz": 2402, "rate": "1M", "per_pct": 2.0,
             "packets_ok": 100, "packets_err": 2, "packets_total": 100},
            {"channel": 2, "freq_mhz": 2404, "rate": "1M", "per_pct": 0.0,
             "packets_ok": 100, "packets_err": 0, "packets_total": 100},
        ],
    )
    html = path.read_text(encoding="utf-8")
    # The first occurrence is in the Best Candidates table.
    assert html.index("<td>2</td>") < html.index("<td>1</td>")


def test_cli_real_tx_fails_before_backend_without_antenna_confirmation(capsys):
    rc = main(["tx", "--duration", "1"])
    assert rc == 2
    assert "interlocked" in capsys.readouterr().err


class FakePort:
    def __init__(self, device, description="USB serial", manufacturer=None,
                 vid=None, hwid=""):
        self.device = device
        self.description = description
        self.manufacturer = manufacturer
        self.vid = vid
        self.hwid = hwid


def test_macos_serial_auto_detection_uses_callout_device(monkeypatch):
    monkeypatch.setattr(serial_console.sys, "platform", "darwin")
    records = [
        FakePort("/dev/tty.usbserial-0001"),
        FakePort("/dev/cu.Bluetooth-Incoming-Port", "Bluetooth-Incoming-Port"),
        FakePort("/dev/cu.usbserial-0001"),
    ]
    assert serial_console.resolve_port("auto", records) == "/dev/cu.usbserial-0001"


def test_serial_auto_detection_refuses_ambiguous_macos_ports(monkeypatch):
    monkeypatch.setattr(serial_console.sys, "platform", "darwin")
    records = [
        FakePort("/dev/cu.usbserial-A"),
        FakePort("/dev/cu.usbmodem-B"),
    ]
    with pytest.raises(BackendError, match="More than one"):
        serial_console.resolve_port("auto", records)


def test_windows_serial_auto_detection_prefers_usb_over_builtin_com(monkeypatch):
    monkeypatch.setattr(serial_console.sys, "platform", "win32")
    records = [
        FakePort("COM1", "Communications Port"),
        FakePort("COM5", "USB Serial Port", vid=0x1234),
    ]
    assert serial_console.resolve_port("auto", records) == "COM5"


def test_serial_cli_uses_configured_port_and_baud(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "config.yaml"
    settings.write_text(
        "serial:\n  port: /dev/cu.usbserial-PAMIR\n  baud: 1500000\n",
        encoding="utf-8",
    )
    seen = []
    monkeypatch.setattr(
        serial_console, "status",
        lambda port, baud: seen.append((port, baud)) or "device ok",
    )
    assert main(["--config", str(settings), "serial", "status"]) == 0
    assert seen == [("/dev/cu.usbserial-PAMIR", 1_500_000)]
    assert "device ok" in capsys.readouterr().out


def test_serial_archive_transfer_verifies_and_unpacks(tmp_path, monkeypatch):
    archive = tmp_path / "tis-tester.tar.gz"
    archive.write_bytes(b"safe offline archive")
    commands = []

    class FakeWire:
        is_open = True

        def write(self, data):
            commands.append(("wire", data))

        def flush(self):
            pass

    class FakeConsole:
        def __init__(self, port, baud, timeout=10):
            self.serial = FakeWire()

        def open(self):
            pass

        def interrupt(self):
            pass

        def command(self, command, timeout=None):
            commands.append(("command", command))
            return "ok", 0

        def close(self):
            self.serial.is_open = False

    monkeypatch.setattr(serial_console, "SerialConsole", FakeConsole)
    progress = []
    result = serial_console.send_archive(
        "ignored", 1_500_000, archive,
        progress=lambda done, total: progress.append((done, total)),
    )
    sent = "\n".join(value for kind, value in commands if kind == "command")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert digest in sent
    assert "sha256sum" in sent
    assert "tar xzf" in sent
    assert progress[-1][0] == progress[-1][1]
    assert "Installed" in result
