"""ReconForge public package interface."""

from reconforge.batch import (
    BATCH_MANIFEST_VERSION,
    BatchPair,
    BatchPlan,
    BatchResult,
    discover_directory_batch,
    load_batch_manifest,
    reconcile_batch,
)
from reconforge.batch_checkpoint import (
    BATCH_CHECKPOINT_VERSION,
    ResumableBatchResult,
    pair_fingerprint,
    reconcile_batch_resumable,
)
from reconforge.csvio import CsvReadOptions, read_csv_dataset
from reconforge.errors import RunNotFoundError, RunStoreError
from reconforge.exporters import (
    ReportFormat,
    infer_report_format,
    render_report,
    write_report,
)
from reconforge.model import Dataset, ReconciliationResult, Record, ValidationViolation
from reconforge.normalization import FieldNormalizer, NormalizerKind
from reconforge.policies import ComparisonPolicy, ComparisonPolicyKind
from reconforge.project_config import (
    PROJECT_CONFIG_VERSION,
    ProjectConfig,
    ProjectConfigError,
    load_project_config,
)
from reconforge.query import (
    IssueKind,
    IssueSort,
    ResultFacets,
    ResultIssue,
    ResultPage,
    ResultQuery,
    SourceRow,
    SourceRowRange,
    apply_page_cursor,
    issue_fingerprint,
    next_page_cursor,
    query_result,
    result_issue_to_dict,
    result_issues,
    result_page_to_dict,
)
from reconforge.query_exporters import (
    render_result_page,
    render_result_page_csv,
    render_result_page_html,
    write_result_page,
)
from reconforge.readers import DatasetFormat, DatasetReadOptions, read_dataset
from reconforge.reconcile import ReconciliationConfig, reconcile
from reconforge.report import load_json_report
from reconforge.result_delta import (
    DeltaIssue,
    DeltaPage,
    DeltaQuery,
    DeltaStatus,
    ResultDelta,
    compare_results,
    filter_delta_issues,
    query_delta,
)
from reconforge.run_store import (
    RUN_STORE_SCHEMA_VERSION,
    RunInput,
    RunStore,
    StoredRun,
    stored_run_to_dict,
)
from reconforge.validation import (
    AllowedValuesRule,
    NumericRangeRule,
    RequiredRule,
    TextPatternRule,
    ValidationRule,
    ValidationRuleKind,
)

__all__ = [
    "BATCH_MANIFEST_VERSION",
    "BATCH_CHECKPOINT_VERSION",
    "BatchPair",
    "BatchPlan",
    "BatchResult",
    "CsvReadOptions",
    "AllowedValuesRule",
    "Dataset",
    "DatasetFormat",
    "DatasetReadOptions",
    "DeltaIssue",
    "DeltaPage",
    "DeltaQuery",
    "DeltaStatus",
    "FieldNormalizer",
    "ComparisonPolicy",
    "ComparisonPolicyKind",
    "PROJECT_CONFIG_VERSION",
    "NormalizerKind",
    "NumericRangeRule",
    "ProjectConfig",
    "ProjectConfigError",
    "Record",
    "ResultIssue",
    "ResultDelta",
    "ResultFacets",
    "ResultPage",
    "ResultQuery",
    "ReportFormat",
    "ReconciliationConfig",
    "ReconciliationResult",
    "ResumableBatchResult",
    "RequiredRule",
    "RUN_STORE_SCHEMA_VERSION",
    "RunInput",
    "RunNotFoundError",
    "RunStore",
    "RunStoreError",
    "StoredRun",
    "SourceRow",
    "SourceRowRange",
    "apply_page_cursor",
    "issue_fingerprint",
    "next_page_cursor",
    "TextPatternRule",
    "ValidationRule",
    "ValidationRuleKind",
    "ValidationViolation",
    "IssueKind",
    "IssueSort",
    "read_csv_dataset",
    "read_dataset",
    "query_result",
    "render_report",
    "render_result_page",
    "render_result_page_csv",
    "render_result_page_html",
    "result_page_to_dict",
    "result_issue_to_dict",
    "result_issues",
    "discover_directory_batch",
    "load_batch_manifest",
    "load_json_report",
    "load_project_config",
    "reconcile",
    "compare_results",
    "filter_delta_issues",
    "query_delta",
    "reconcile_batch",
    "reconcile_batch_resumable",
    "pair_fingerprint",
    "stored_run_to_dict",
    "infer_report_format",
    "write_report",
    "write_result_page",
]

__version__ = "0.1.0"
