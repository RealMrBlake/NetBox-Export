import sys
import types

from django.conf import settings

if not settings.configured:
    settings.configure(
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        INSTALLED_APPS=["django.contrib.contenttypes", "netbox_export"],
        SECRET_KEY="tests",
    )

netbox_module = types.ModuleType("netbox")
plugins_module = types.ModuleType("netbox.plugins")


class PluginConfig:
    pass


plugins_module.PluginConfig = PluginConfig
netbox_module.plugins = plugins_module
sys.modules.setdefault("netbox", netbox_module)
sys.modules.setdefault("netbox.plugins", plugins_module)

import django

django.setup()
