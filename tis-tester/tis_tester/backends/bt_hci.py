"""Bluetooth Direct Test Mode backend using standard HCI commands.

Uses BlueZ's `hcitool cmd` so it works on any Linux with a raw HCI socket --
no vendor tool required (BT_EN + UART/USB HCI per the FCS960K-N design).

Commands used (Bluetooth Core Spec, Vol 4 Part E):
    LE Receiver Test [v1]      OGF 0x08 OCF 0x001D  (rx_channel)
    LE Receiver Test [v2]      OGF 0x08 OCF 0x0033  (rx_channel, phy, mod_idx)
    LE Transmitter Test [v1]   OGF 0x08 OCF 0x001E  (tx_channel, len, payload)
    LE Transmitter Test [v2]   OGF 0x08 OCF 0x0034  (+ phy)
    LE Test End                OGF 0x08 OCF 0x001F  -> returns packet count

Notes for TIS work:
  * Packet count comes from LE Test End (spec-mandated), so live counters
    are approximated by periodically ending + restarting the test unless a
    vendor "read counters" command is configured. For the standard TIS
    procedure (instrument sends a fixed burst, then you end the test and
    read the count) the v1/v2 flow is exactly right.
  * RSSI during DTM and CRC-error counts are NOT exposed by standard HCI;
    supply vendor_rssi_cmd / adjust as needed for the AIC8800 vendor OCFs.
  * PER = 1 - received/expected, with expected = burst count configured on
    the CMW/anechoic instrument.
"""

from __future__ import annotations
import re

from .base import RadioBackend, RxStats, TestParams, BackendError, run_argv
from .. import rates

OGF_LE = 0x08
OCF_RX_V1 = 0x001D
OCF_TX_V1 = 0x001E
OCF_TEST_END = 0x001F
OCF_RX_V2 = 0x0033
OCF_TX_V2 = 0x0034
OGF_HOST = 0x03
OCF_RESET = 0x0003


class BtHciBackend(RadioBackend):
    name = "bt-hci"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        b = cfg["bt"]
        self.dev = b["hci_dev"]
        self.enhanced = bool(b.get("use_enhanced_test", True))
        self.vendor_pwr = b.get("vendor_tx_power_cmd") or ""
        self.vendor_rssi = b.get("vendor_rssi_cmd") or ""
        self.vendor_rssi_re = re.compile(b.get("vendor_rssi_regex",
                                                r"rssi\s*[:=]\s*(-?\d+)"), re.I)
        self._accum_ok = 0            # packets accumulated across restarts
        self._live = False

    # ------------------------------------------------------------------ HCI
    def _hci_cmd(self, ogf: int, ocf: int, params: bytes = b"") -> bytes:
        argv = ["hcitool", "-i", self.dev, "cmd",
                f"0x{ogf:02x}", f"0x{ocf:04x}"] + [f"0x{b:02x}" for b in params]
        out = run_argv(argv)
        return self._parse_event(out)

    @staticmethod
    def _parse_event(out: str) -> bytes:
        """hcitool prints 'HCI Event: 0x0e plen N' followed by hex bytes."""
        hexbytes: list[int] = []
        grab = False
        for line in out.splitlines():
            if "HCI Event" in line:
                grab = True
                continue
            if grab:
                for tok in line.split():
                    if re.fullmatch(r"[0-9A-Fa-f]{2}", tok):
                        hexbytes.append(int(tok, 16))
        return bytes(hexbytes)

    def _check_status(self, evt: bytes, what: str) -> bytes:
        # Command Complete payload: num_hci_cmd(1) opcode(2) status(1) [ret...]
        if len(evt) < 4:
            raise BackendError(f"{what}: unexpected HCI response {evt.hex()}")
        status = evt[3]
        if status != 0x00:
            raise BackendError(f"{what}: controller returned status 0x{status:02x}")
        return evt[4:]

    def _ensure_up(self) -> None:
        run_argv(["hciconfig", self.dev, "up"])

    # ------------------------------------------------------------- lifecycle
    def open(self) -> None:
        self._ensure_up()
        self._check_status(self._hci_cmd(OGF_HOST, OCF_RESET), "HCI Reset")

    # --------------------------------------------------------------- control
    def _validate(self, p: TestParams) -> None:
        if p.channel not in rates.BLE_CHANNELS:
            raise BackendError("BLE RF channel must be 0-39 "
                               f"(got {p.channel}; freq = 2402 + 2*ch)")
        if p.rate not in rates.BT_PHYS:
            raise BackendError(f"BT PHY must be one of {list(rates.BT_PHYS)}")
        if p.payload not in rates.BT_PAYLOADS:
            raise BackendError(f"payload must be one of {list(rates.BT_PAYLOADS)}")
        if not (0 <= p.payload_len <= 255):
            raise BackendError("payload_len must be 0-255")

    def _set_vendor_power(self, p: TestParams) -> None:
        if self.vendor_pwr:
            run_argv(["sh", "-c", self.vendor_pwr.format(dev=self.dev,
                                                         power=p.tx_power_dbm)])

    def start_tx(self, p: TestParams) -> None:
        self._validate(p)
        if self.mode:
            self.stop()
        self._set_vendor_power(p)
        phy = rates.BT_PHYS[p.rate].hci_phy
        pl = rates.BT_PAYLOADS[p.payload]
        if self.enhanced:
            evt = self._hci_cmd(OGF_LE, OCF_TX_V2,
                                bytes([p.channel, p.payload_len, pl, phy]))
        else:
            evt = self._hci_cmd(OGF_LE, OCF_TX_V1,
                                bytes([p.channel, p.payload_len, pl]))
        self._check_status(evt, "LE Transmitter Test")
        self.mode, self.params = "tx", p

    def start_rx(self, p: TestParams) -> None:
        self._validate(p)
        if self.mode:
            self.stop()
        self._start_rx_raw(p)
        self.mode, self.params = "rx", p
        self._accum_ok = 0
        self._live = True

    def _start_rx_raw(self, p: TestParams) -> None:
        phy = rates.BT_PHYS[p.rate].hci_phy
        if self.enhanced:
            # modulation_index 0x00 = standard
            evt = self._hci_cmd(OGF_LE, OCF_RX_V2, bytes([p.channel, phy, 0x00]))
        else:
            evt = self._hci_cmd(OGF_LE, OCF_RX_V1, bytes([p.channel]))
        self._check_status(evt, "LE Receiver Test")

    def _test_end(self) -> int:
        ret = self._check_status(self._hci_cmd(OGF_LE, OCF_TEST_END),
                                 "LE Test End")
        if len(ret) < 2:
            raise BackendError("LE Test End returned no packet count")
        return ret[0] | (ret[1] << 8)

    def stop(self) -> RxStats | None:
        if self.mode == "tx":
            self._test_end()
            self.mode = None
            return None
        if self.mode == "rx":
            n = self._test_end()
            self._live = False
            self.mode = None
            total = self._accum_ok + n
            return RxStats(packets_ok=total,
                           packets_err=None,
                           rssi_dbm=self._vendor_rssi(),
                           expected_packets=self.params.expected_packets
                           if self.params else None)
        return None

    def reset_rx_counters(self) -> None:
        if self.mode == "rx":
            self._test_end()
            self._start_rx_raw(self.params)
            self._accum_ok = 0

    # ----------------------------------------------------------------- stats
    def _vendor_rssi(self) -> float | None:
        if not self.vendor_rssi:
            return None
        try:
            out = run_argv(["sh", "-c", self.vendor_rssi.format(dev=self.dev)])
            m = self.vendor_rssi_re.search(out)
            return float(m.group(1)) if m else None
        except BackendError:
            return None

    def poll_rx(self) -> RxStats:
        """Live counter read.

        Standard HCI only reports the count at Test End, so we cycle
        end->restart to sample. The 16-bit counter also wraps at 65535,
        so periodic cycling doubles as overflow protection during long
        TIS dwells.
        """
        if self.mode != "rx":
            raise BackendError("Receiver is not running")
        n = self._test_end()
        self._accum_ok += n
        self._start_rx_raw(self.params)
        return RxStats(packets_ok=self._accum_ok,
                       packets_err=None,
                       rssi_dbm=self._vendor_rssi(),
                       expected_packets=self.params.expected_packets)
