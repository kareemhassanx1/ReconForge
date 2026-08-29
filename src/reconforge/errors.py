"""Domain-specific exceptions exposed by ReconForge."""


class ReconForgeError(Exception):
    """Base class for errors that can be presented directly to a user."""


class InputError(ReconForgeError):
    """Raised when an input file cannot be interpreted as a dataset."""


class ConfigurationError(ReconForgeError):
    """Raised when reconciliation settings cannot be applied to the inputs."""


class BatchError(ReconForgeError):
    """Raised when a batch plan cannot be discovered or loaded safely."""


class BatchCheckpointError(BatchError):
    """Raised when a resumable batch checkpoint is invalid or cannot be saved."""


class RunStoreError(ReconForgeError):
    """Raised when reconciliation history cannot be persisted or queried."""


class RunNotFoundError(RunStoreError):
    """Raised when a requested stored run does not exist."""
