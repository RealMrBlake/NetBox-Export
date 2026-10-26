from dcim.models import Location, Region, Site
from django import forms
from django.utils.translation import gettext_lazy as _
from tenancy.models import Tenant, TenantGroup


class ExportForm(forms.Form):
    scope_type = forms.ChoiceField(
        label=_("Export scope"),
        choices=(
            ("tenant_group", _("Tenant group")),
            ("tenant", _("Tenant")),
            ("region", _("Region")),
            ("site", _("Site")),
            ("location", _("Location")),
        ),
    )
    tenant_group = forms.ModelChoiceField(
        label=_("Tenant group"), queryset=TenantGroup.objects.all(), required=False
    )
    tenant = forms.ModelChoiceField(label=_("Tenant"), queryset=Tenant.objects.all(), required=False)
    region = forms.ModelChoiceField(label=_("Region"), queryset=Region.objects.all(), required=False)
    site = forms.ModelChoiceField(label=_("Site"), queryset=Site.objects.all(), required=False)
    location = forms.ModelChoiceField(label=_("Location"), queryset=Location.objects.all(), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tenant"].label_from_instance = lambda obj: f"{obj.group or '-'} / {obj}"
        self.fields["location"].label_from_instance = lambda obj: f"{obj.site} / {obj}"

    def clean(self):
        cleaned = super().clean()
        scope_type = cleaned.get("scope_type")
        selected = cleaned.get(scope_type) if scope_type else None
        if scope_type and selected is None:
            self.add_error(scope_type, _("Select an object for the export scope."))
        cleaned["scope_object"] = selected
        return cleaned


class ImportForm(forms.Form):
    archive = forms.FileField(
        label=_("NetBox-Export archive"),
        widget=forms.ClearableFileInput(attrs={"accept": ".zip,application/zip"}),
    )
    conflict_strategy = forms.ChoiceField(
        label=_("Existing objects"),
        choices=(
            ("update", _("Update")),
            ("skip", _("Skip")),
            ("fail", _("Abort import")),
        ),
        initial="update",
    )
    dry_run = forms.BooleanField(
        label=_("Check only (no changes)"),
        required=False,
        initial=True,
        help_text=_("Runs the complete import and then rolls back the database transaction."),
    )
    force = forms.BooleanField(
        label=_("Force mode (skip errors)"),
        required=False,
        initial=False,
        help_text=_(
            "Objects that fail to import are skipped individually instead of rolling back the whole import. "
            "Skipped objects are listed as warnings in the report."
        ),
    )
    confirm_apply = forms.BooleanField(
        label=_("I confirm the writing import"),
        required=False,
    )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("dry_run") and not cleaned.get("confirm_apply"):
            self.add_error("confirm_apply", _("Confirmation is required for a writing import."))
        return cleaned
