"""Calibre-side preferences and state locations."""

from dataclasses import asdict
from pathlib import Path

from calibre.utils.config import JSONConfig, config_dir

from .xteink.settings import BALANCED, CUSTOM, PRESETS, options_from_dict
from .xteink.transfer import DEFAULT_TEMPLATE, TransferOptions


PREFS = JSONConfig("plugins/xteink_x4_pro")
DEFAULTS = {
    "profile": BALANCED,
    "custom": asdict(PRESETS[BALANCED]),
    "presets": {},
    "automatically_optimise": True,
    "enable_transfer": True,
    "export_dir": "",
    "usb_dir": "",
    "detect_device": True,
    "temporary": "system",
    "cache": True,
    "logging": False,
    "template": DEFAULT_TEMPLATE,
    "structure": "author",
    "overwrite": "keep_both",
    "epubcheck_path": "",
}
PREFS.defaults.update(DEFAULTS)
STATE_DIR = Path(config_dir) / "plugins" / "xteink_x4_pro"


def get_profiles():
    profiles = dict(PRESETS)
    profiles[CUSTOM] = options_from_dict(PREFS["custom"])
    profiles.update({name: options_from_dict(values) for name, values in PREFS["presets"].items()})
    return profiles


def get_transfer_options():
    return TransferOptions(PREFS["template"], PREFS["structure"], PREFS["overwrite"])
