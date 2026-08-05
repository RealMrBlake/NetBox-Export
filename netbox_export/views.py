from django.contrib.auth.mixins import UserPassesTestMixin
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.text import slugify
from django.views import View
from netbox.plugins import get_plugin_config

from .forms import ExportForm, ImportForm
from .services.archive import read_archive
from .services.exceptions import ExportImportError
from .services.exporter import export_scope
from .services.importer import import_archive


class DashboardView(UserPassesTestMixin, View):
    template_name = "netbox_export/dashboard.html"
    raise_exception = True

    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

    def _context(self, export_form=None, import_form=None, report=None, manifest=None):
        return {
            "export_form": export_form or ExportForm(prefix="export"),
            "import_form": import_form or ImportForm(prefix="import"),
            "report": report,
            "manifest": manifest,
        }

    def get(self, request):
        return render(request, self.template_name, self._context())

    def post(self, request):
        action = request.POST.get("action")
        if action == "export":
            return self._export(request)
        if action == "import":
            return self._import(request)
        return render(request, self.template_name, self._context(), status=400)

    def _export(self, request):
        form = ExportForm(request.POST, prefix="export")
        if not form.is_valid():
            return render(request, self.template_name, self._context(export_form=form), status=400)
        scope = form.cleaned_data["scope_object"]
        try:
            payload, _ = export_scope(
                form.cleaned_data["scope_type"],
                scope.pk,
                max_objects=int(get_plugin_config("netbox_export", "max_objects", 50000)),
                signing_key=get_plugin_config("netbox_export", "archive_signing_key", ""),
            )
        except ExportImportError as exc:
            form.add_error(None, str(exc))
            return render(request, self.template_name, self._context(export_form=form), status=400)

        filename = f"netbox-export-{form.cleaned_data['scope_type']}-{slugify(str(scope)) or scope.pk}.zip"
        response = HttpResponse(payload, content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response

    def _import(self, request):
        form = ImportForm(request.POST, request.FILES, prefix="import")
        if not form.is_valid():
            return render(request, self.template_name, self._context(import_form=form), status=400)
        max_size = int(get_plugin_config("netbox_export", "max_archive_size_mb", 250)) * 1024 * 1024
        try:
            parsed = read_archive(
                form.cleaned_data["archive"],
                max_size=max_size,
                max_objects=int(get_plugin_config("netbox_export", "max_objects", 50000)),
                signing_key=get_plugin_config("netbox_export", "archive_signing_key", ""),
            )
            report = import_archive(
                parsed,
                conflict_strategy=form.cleaned_data["conflict_strategy"],
                dry_run=form.cleaned_data["dry_run"],
            )
        except ExportImportError as exc:
            form.add_error(None, str(exc))
            return render(request, self.template_name, self._context(import_form=form), status=400)
        return render(
            request,
            self.template_name,
            self._context(import_form=ImportForm(prefix="import"), report=report, manifest=parsed.manifest),
        )

