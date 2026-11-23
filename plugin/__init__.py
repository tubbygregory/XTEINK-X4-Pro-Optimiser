"""Calibre plugin registration and preferences entry point."""

from calibre.customize import InterfaceActionBase

from .xteink import VERSION


class XteinkX4ProOptimiser(InterfaceActionBase):
    name = "XTEINK X4 Pro Optimiser"
    description = "Prepare and export EPUB copies for the XTEINK X4 Pro running Licorice."
    supported_platforms = ["windows", "osx", "linux"]
    author = "Gregory Tubby"
    version = VERSION
    minimum_calibre_version = (9, 19, 0)
    actual_plugin = "calibre_plugins.xteink_x4_pro.action:XteinkAction"

    def is_customizable(self):
        return True

    def config_widget(self):
        from .config import ConfigWidget
        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.save_settings()
