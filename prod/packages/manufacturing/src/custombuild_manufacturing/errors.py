class ManufacturingError(RuntimeError):
    """Base error for deterministic manufacturing failures."""


class NestingError(ManufacturingError):
    pass


class ProductionBlockedError(ManufacturingError):
    def __init__(self, message: str, *, report: object | None = None) -> None:
        super().__init__(message)
        self.report = report


class ArtifactError(ManufacturingError):
    pass
