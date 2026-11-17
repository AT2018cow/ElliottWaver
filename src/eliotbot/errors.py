class EliotError(Exception):
    """An actionable failure safe to display without request secrets."""

    exit_code = 5
    code = "ENGINE_ERROR"


class InputError(EliotError):
    exit_code = 2
    code = "INVALID_INPUT"


class DataError(EliotError):
    exit_code = 3
    code = "INSUFFICIENT_DATA"


class SourceError(EliotError):
    exit_code = 4
    code = "SOURCE_UNAVAILABLE"


class ExportError(EliotError):
    exit_code = 6
    code = "EXPORT_FAILED"
