from __future__ import annotations

from .base import RadioBackend, RxStats, TestParams, BackendError
from .wifi_aic import WifiAicBackend
from .bt_hci import BtHciBackend
from .bt_aic_uart import BtAicUartBackend
from .mock import MockBackend

__all__ = ["RadioBackend", "RxStats", "TestParams", "BackendError",
           "make_backend"]


def make_backend(radio: str, cfg: dict, force_mock: bool = False) -> RadioBackend:
    radio = radio.lower()
    if radio not in ("wifi", "bt"):
        raise ValueError("radio must be 'wifi' or 'bt'")
    kind = cfg[radio].get("backend", "shell" if radio == "wifi" else "aic_uart")
    if force_mock or kind == "mock":
        return MockBackend(cfg, radio)
    if radio == "wifi":
        return WifiAicBackend(cfg)
    if kind == "aic_uart":
        return BtAicUartBackend(cfg)
    return BtHciBackend(cfg)
