from __future__ import annotations

import copy
import time
from pathlib import Path

import pytest

from tis_tester import rates
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


def test_rate_and_channel_validation():
    assert rates.channel_to_freq("2.4GHz", 6) == 2437
    assert rates.channel_to_freq("5GHz", 36) == 5180
    with pytest.raises(ValueError):
        rates.channel_to_freq("5GHz", 6)


def test_unsafe_private_command_is_blocked_before_ioctl():
    client = AicPrivateIoctl("wlan0")
    with pytest.raises(BackendError, match="Blocked unsafe"):
        client.command("set_freq_cal", 1)


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


def test_bt_aic_uart_selected_by_default(tmp_path):
    backend = make_backend("bt", config(tmp_path))
    assert isinstance(backend, BtAicUartBackend)


def test_bt_aic_uart_requires_1m_phy(tmp_path):
    backend = BtAicUartBackend(config(tmp_path))
    with pytest.raises(BackendError, match="validated for BLE 1M PHY"):
        backend._validate(Params(radio="bt", channel=19, rate="2M"))


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


def test_cli_real_tx_fails_before_backend_without_antenna_confirmation(capsys):
    rc = main(["tx", "--duration", "1"])
    assert rc == 2
    assert "interlocked" in capsys.readouterr().err
