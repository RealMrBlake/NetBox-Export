from typing import ClassVar

from netbox.plugins import PluginConfig


class NetBoxExportConfig(PluginConfig):
    name = "netbox_export"
    verbose_name = "NetBox-Export"
    description = "Portable ZIP export and import for tenants and locations"
    version = "0.3.1"
    author = "NetBox Export contributors"
    base_url = "netbox-export"
    min_version = "4.6.0"
    max_version = "4.6.99"
    required_settings: ClassVar[list[str]] = []
    default_settings: ClassVar[dict] = {
        "max_objects": 50000,
        "max_archive_size_mb": 250,
        "query_batch_size": 500,
        "archive_signing_key": "",
    }


config = NetBoxExportConfig
