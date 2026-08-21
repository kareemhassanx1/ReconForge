"""ReconForge public package interface."""

from reconforge.csvio import CsvReadOptions, read_csv_dataset
from reconforge.model import Dataset, ReconciliationResult, Record
from reconforge.normalization import FieldNormalizer, NormalizerKind
from reconforge.reconcile import ReconciliationConfig, reconcile

__all__ = [
    "CsvReadOptions",
    "Dataset",
    "FieldNormalizer",
    "NormalizerKind",
    "Record",
    "ReconciliationConfig",
    "ReconciliationResult",
    "read_csv_dataset",
    "reconcile",
]

__version__ = "0.1.0"
