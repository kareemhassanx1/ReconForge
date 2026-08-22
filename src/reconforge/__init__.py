"""ReconForge public package interface."""

from reconforge.csvio import CsvReadOptions, read_csv_dataset
from reconforge.model import Dataset, ReconciliationResult, Record
from reconforge.normalization import FieldNormalizer, NormalizerKind
from reconforge.readers import DatasetFormat, DatasetReadOptions, read_dataset
from reconforge.reconcile import ReconciliationConfig, reconcile

__all__ = [
    "CsvReadOptions",
    "Dataset",
    "DatasetFormat",
    "DatasetReadOptions",
    "FieldNormalizer",
    "NormalizerKind",
    "Record",
    "ReconciliationConfig",
    "ReconciliationResult",
    "read_csv_dataset",
    "read_dataset",
    "reconcile",
]

__version__ = "0.1.0"
