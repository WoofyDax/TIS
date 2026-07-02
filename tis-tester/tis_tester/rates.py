"""RF parameter tables for the FCS960K-N (AIC8800D80 U02).

Values are taken from the Quectel FCS960K-N Hardware Design v1.0.0:
  * 2.4 GHz: 2.400-2.4835 GHz, 5 GHz: 5.150-5.850 GHz
  * 802.11b 1/2/5.5/11, 802.11a/g 6-54
  * 802.11n  HT20/HT40  MCS 0-7
  * 802.11ac VHT20/VHT40 MCS 0-9   (5 GHz only)
  * 802.11ax HE20/HE40  MCS 0-11
  * TX power 0-23 dBm
  * BT: BR, EDR2 (pi/4-DQPSK), EDR3 (8-DPSK), BLE 1M/2M, Coded S=2/S=8
"""

from __future__ import annotations
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Bands / channels
# --------------------------------------------------------------------------

BANDS = ("2.4GHz", "5GHz")

# 2.4 GHz: ch 1-13 -> 2412 + 5*(ch-1);  ch 14 -> 2484 (11b Japan only)
CHANNELS_24 = {ch: 2412 + 5 * (ch - 1) for ch in range(1, 14)}
CHANNELS_24[14] = 2484

# 5 GHz: freq = 5000 + 5*ch  (UNII-1/2A/2C/3, per datasheet 5.150-5.850)
_5G_CH = (list(range(36, 65, 4)) + list(range(100, 145, 4)) +
          list(range(149, 166, 4)))
CHANNELS_5 = {ch: 5000 + 5 * ch for ch in _5G_CH}


def channels_for_band(band: str) -> dict[int, int]:
    return CHANNELS_24 if band.startswith("2.4") else CHANNELS_5


def channel_to_freq(band: str, channel: int) -> int:
    table = channels_for_band(band)
    if channel not in table:
        raise ValueError(f"Channel {channel} not valid for {band}. "
                         f"Valid: {sorted(table)}")
    return table[channel]


BANDWIDTHS_MHZ = (20, 40)

# --------------------------------------------------------------------------
# Wi-Fi rates
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WifiRate:
    name: str          # e.g. "11b-1M", "OFDM-54M", "HT-MCS7", "HE-MCS11"
    family: str        # "legacy-b" | "legacy-ag" | "HT" | "VHT" | "HE"
    index: int         # legacy: rate in units of 100kbps trick avoided; MCS: mcs index
    code: int          # generic rate code passed to the vendor tool
    bands: tuple[str, ...] = BANDS


# Vendor rate codes: the AIC8800 test firmware uses a linear rate index
# (0-3 = 11b, 4-11 = 11a/g OFDM, 128+mcs = HT, 256+mcs = VHT, 512+mcs = HE).
# These are the defaults used in the {rate_code} template placeholder and
# can be remapped in tis_config.yaml -> wifi.rate_code_overrides.

LEGACY_B = [WifiRate(f"11b-{r}M", "legacy-b", i, i, ("2.4GHz",))
            for i, r in enumerate(("1", "2", "5.5", "11"))]

_AG = (6, 9, 12, 18, 24, 36, 48, 54)
LEGACY_AG = [WifiRate(f"OFDM-{r}M", "legacy-ag", i, 4 + i) for i, r in enumerate(_AG)]

HT = [WifiRate(f"HT-MCS{m}", "HT", m, 128 + m) for m in range(8)]
VHT = [WifiRate(f"VHT-MCS{m}", "VHT", m, 256 + m, ("5GHz",)) for m in range(10)]
HE = [WifiRate(f"HE-MCS{m}", "HE", m, 512 + m) for m in range(12)]

ALL_WIFI_RATES: list[WifiRate] = LEGACY_B + LEGACY_AG + HT + VHT + HE
WIFI_RATE_BY_NAME = {r.name.lower(): r for r in ALL_WIFI_RATES}


def wifi_rates_for(band: str) -> list[WifiRate]:
    return [r for r in ALL_WIFI_RATES if band in r.bands]


def lookup_wifi_rate(name: str) -> WifiRate:
    r = WIFI_RATE_BY_NAME.get(name.lower())
    if r is None:
        raise ValueError(f"Unknown Wi-Fi rate '{name}'. "
                         f"Examples: 11b-1M, OFDM-6M, HT-MCS7, VHT-MCS9, HE-MCS11")
    return r


TX_POWER_MIN_DBM = 0
TX_POWER_MAX_DBM = 23

# --------------------------------------------------------------------------
# Bluetooth
# --------------------------------------------------------------------------

# BLE RF channel index 0-39; freq = 2402 + 2*ch
BLE_CHANNELS = {ch: 2402 + 2 * ch for ch in range(40)}

@dataclass(frozen=True)
class BtPhy:
    name: str
    hci_phy: int       # value for HCI LE Rx/Tx Test v2+ PHY parameter

BT_PHYS = {
    "1M":      BtPhy("BLE 1 Mbps", 0x01),
    "2M":      BtPhy("BLE 2 Mbps", 0x02),
    "coded-s8": BtPhy("BLE Coded S=8 (125 kbps)", 0x03),
    "coded-s2": BtPhy("BLE Coded S=2 (500 kbps)", 0x04),
}

# HCI LE Transmitter Test payload types
BT_PAYLOADS = {
    "prbs9": 0x00, "11110000": 0x01, "10101010": 0x02, "prbs15": 0x03,
    "11111111": 0x04, "00000000": 0x05, "00001111": 0x06, "01010101": 0x07,
}
