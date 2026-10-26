from django.utils.translation import gettext_lazy as _
from netbox.plugins import PluginMenuItem

menu_items = (
    PluginMenuItem(
        link="plugins:netbox_export:dashboard",
        link_text=_("Export / Import"),
        staff_only=True,
    ),
)

