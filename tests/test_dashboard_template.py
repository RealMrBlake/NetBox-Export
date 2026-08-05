from pathlib import Path


def test_form_action_field_cannot_shadow_request_target():
    template = (
        Path(__file__).parents[1]
        / "netbox_export"
        / "templates"
        / "netbox_export"
        / "dashboard.html"
    ).read_text(encoding="utf-8")

    assert "form.getAttribute('action')" in template
    assert "exportForm.action" not in template
    assert "importForm.action" not in template
