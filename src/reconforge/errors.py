"""Domain-specific exceptions exposed by ReconForge."""


class ReconForgeError(Exception):
    """Base class for errors that can be presented directly to a user."""


class InputError(ReconForgeError):
    """Raised when an input file cannot be interpreted as a dataset."""


class ConfigurationError(ReconForgeError):
    """Raised when reconciliation settings cannot be applied to the inputs."""
