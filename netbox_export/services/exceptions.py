class ExportImportError(Exception):
    """Base class for errors safe to present in the UI."""


class ArchiveValidationError(ExportImportError):
    pass


class GraphLimitError(ExportImportError):
    pass


class ImportConflictError(ExportImportError):
    pass

