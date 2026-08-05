from dcim.models import Location, Region, Site
from django import forms
from tenancy.models import Tenant, TenantGroup


class ExportForm(forms.Form):
    scope_type = forms.ChoiceField(
        label="Exportbereich",
        choices=(
            ("tenant_group", "Mandantengruppe"),
            ("tenant", "Mandant"),
            ("region", "Region"),
            ("site", "Standort"),
            ("location", "Lokation"),
        ),
    )
    tenant_group = forms.ModelChoiceField(
        label="Mandantengruppe", queryset=TenantGroup.objects.all(), required=False
    )
    tenant = forms.ModelChoiceField(label="Mandant", queryset=Tenant.objects.all(), required=False)
    region = forms.ModelChoiceField(label="Region", queryset=Region.objects.all(), required=False)
    site = forms.ModelChoiceField(label="Standort", queryset=Site.objects.all(), required=False)
    location = forms.ModelChoiceField(label="Lokation", queryset=Location.objects.all(), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tenant"].label_from_instance = lambda obj: f"{obj.group or '-'} / {obj}"
        self.fields["location"].label_from_instance = lambda obj: f"{obj.site} / {obj}"

    def clean(self):
        cleaned = super().clean()
        scope_type = cleaned.get("scope_type")
        selected = cleaned.get(scope_type) if scope_type else None
        if scope_type and selected is None:
            self.add_error(scope_type, "Bitte ein Objekt für den Exportbereich auswählen.")
        cleaned["scope_object"] = selected
        return cleaned


class ImportForm(forms.Form):
    archive = forms.FileField(
        label="NetBox-Export-Archiv",
        widget=forms.ClearableFileInput(attrs={"accept": ".zip,application/zip"}),
    )
    conflict_strategy = forms.ChoiceField(
        label="Vorhandene Objekte",
        choices=(
            ("update", "Aktualisieren"),
            ("skip", "Überspringen"),
            ("fail", "Import abbrechen"),
        ),
        initial="update",
    )
    dry_run = forms.BooleanField(
        label="Nur prüfen (keine Änderungen)",
        required=False,
        initial=True,
        help_text="Führt den vollständigen Import aus und setzt die Datenbanktransaktion anschließend zurück.",
    )
    confirm_apply = forms.BooleanField(
        label="Ich bestätige den schreibenden Import",
        required=False,
    )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("dry_run") and not cleaned.get("confirm_apply"):
            self.add_error("confirm_apply", "Für einen schreibenden Import ist die Bestätigung erforderlich.")
        return cleaned
