"""ReconForge public package interface."""

from reconforge.csvio import CsvReadOptions, read_csv_dataset
from reconforge.model import Dataset, ReconciliationResult, Record, ValidationViolation
from reconforge.normalization import FieldNormalizer, NormalizerKind
from reconforge.policies import ComparisonPolicy, ComparisonPolicyKind
from reconforge.project_config import (
    PROJECT_CONFIG_VERSION,
    ProjectConfig,
    ProjectConfigError,
    load_project_config,
)
from reconforge.readers import DatasetFormat, DatasetReadOptions, read_dataset
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.validation import (
    AllowedValuesRule,
    NumericRangeRule,
    RequiredRule,
    TextPatternRule,
    ValidationRule,
    ValidationRuleKind,
)

__all__ = [
    "CsvReadOptions",
    "AllowedValuesRule",
    "Dataset",
    "DatasetFormat",
    "DatasetReadOptions",
    "FieldNormalizer",
    "ComparisonPolicy",
    "ComparisonPolicyKind",
    "PROJECT_CONFIG_VERSION",
    "NormalizerKind",
    "NumericRangeRule",
    "ProjectConfig",
    "ProjectConfigError",
    "Record",
    "ReconciliationConfig",
    "ReconciliationResult",
    "RequiredRule",
    "TextPatternRule",
    "ValidationRule",
    "ValidationRuleKind",
    "ValidationViolation",
    "read_csv_dataset",
    "read_dataset",
    "load_project_config",
    "reconcile",
]

__version__ = "0.1.0"
