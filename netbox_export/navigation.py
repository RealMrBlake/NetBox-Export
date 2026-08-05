from netbox.plugins import PluginMenuItem

menu_items = (
    PluginMenuItem(
        link="plugins:netbox_export:dashboard",
        link_text="Export / Import",
        staff_only=True,
    ),
)

