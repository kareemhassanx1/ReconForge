# ReconForge

ReconForge is a local-first reconciliation tool for tabular business data. It compares CSV,
JSON, and NDJSON exports using stable business keys and produces a deterministic report of
missing records, duplicate keys, schema drift, and field-level value differences.

All built-in readers produce the same immutable dataset model. The project is intentionally
designed so that additional readers, comparison policies, report formats, and persistence can be
added without coupling them to the command-line interface.

## Current capabilities

- Read UTF-8 CSV files with an explicit delimiter or safe delimiter detection.
- Read JSON arrays and newline-delimited JSON (`.ndjson` or `.jsonl`) records.
- Detect formats from file extensions or select each input format explicitly.
- Normalize whitespace and empty values consistently at the input boundary.
- Normalize selected columns as text, integer, decimal, or ISO date values.
- Apply explicit decimal-tolerance, normalized-date, or case-insensitive-text comparisons.
- Load repeatable reconciliation settings from a strictly validated JSON project configuration.
- Validate source data with composable required, allowed-value, numeric-range, and text-pattern rules.
- Reconcile deterministic directory or manifest batches with explicit unmatched-file reporting.
- Resume interrupted batches safely with content-addressed durable checkpoints.
- Persist run metadata and aggregate summaries in a transactional, versioned SQLite history.
- Reconcile rows using one or more key columns.
- Compare selected fields or all shared non-key fields.
- Detect duplicate keys without silently selecting an arbitrary record.
- Report fields that exist on only one side of the comparison.
- Export one canonical report model as stable JSON, analysis-ready CSV, or self-contained HTML.
- Investigate saved JSON results with deterministic filters, sorting, facets, cursors, and exports.
- Compare saved reports to identify added, moved, resolved, and unchanged issues.
- Strictly verify saved single and batch reports with semantic and per-pair fingerprints.
- Return a non-zero CLI status when differences are found.

## Quick start

Create `left.csv`:

```csv
invoice_id,customer,amount,status
INV-100,Northwind,125.00,paid
INV-101,Contoso,80.00,pending
```

Create `right.csv`:

```csv
invoice_id,customer,amount,status
INV-100,Northwind,125.00,paid
INV-101,Contoso,82.50,paid
```

Then install the package and compare them:

```powershell
python -m pip install -e .
reconforge compare left.csv right.csv --key invoice_id --output report.json
```

This example intentionally changes the amount and status for `INV-101`. ReconForge writes the
complete JSON result to `report.json` and returns status `1` because it found differences. It
returns `0` when the inputs reconcile and `2` when an input, configuration, or output error
prevents comparison.

Select specific comparison fields with repeated `--compare` arguments:

```powershell
reconforge compare left.csv right.csv `
  --key invoice_id `
  --compare amount `
  --compare status
```

Use multiple key columns by repeating `--key`:

```powershell
reconforge compare left.csv right.csv --key account_id --key period
```

Normalize source-specific representations before matching or comparing them:

```powershell
reconforge compare accounting.csv gateway.csv `
  --key invoice_id `
  --normalize invoice_id=integer `
  --normalize amount=decimal `
  --normalize settlement_date=date
```

Normalizers are assigned per column and applied to both inputs. `integer` accepts signed base-10
integers, `decimal` accepts finite decimal numbers, and `date` accepts strict `YYYY-MM-DD` calendar
dates. The `text` normalizer applies Unicode compatibility normalization, collapses whitespace,
and performs Unicode-aware case folding. Null values remain null. A bad value stops the comparison
with a deterministic message containing the source, row, field, original value, and expected type.
The JSON report records the effective normalizer for each configured field.

Exact value comparison remains the default. When two systems intentionally represent a field
differently, assign an explicit comparison policy to that column:

```powershell
reconforge compare accounting.csv gateway.csv `
  --key invoice_id `
  --policy amount=decimal:0.01 `
  --policy settlement_date=date `
  --policy customer_name=text
```

The `decimal` policy compares finite decimal values using an inclusive absolute tolerance, so a
difference of exactly `0.01` is accepted by `decimal:0.01`. Omitting the suffix uses zero
tolerance. Decimal arithmetic is used directly, avoiding binary floating-point rounding. The
`date` policy normalizes valid ISO date representations before comparison, and `text` applies
Unicode compatibility normalization, collapses whitespace, and compares with Unicode-aware case
folding. Null only equals null for every policy.

Policies are configured independently per field and run after any configured field normalizer.
They cannot target key or ignored columns. Invalid decimal or date values stop the comparison with
their source, row, field, value, and policy in deterministic order. The JSON report records every
effective policy under `configuration.comparison_policies`.

## Project configuration

Use a versioned JSON file when the same reconciliation needs to be run repeatedly. Input paths
remain command arguments, while formats and reconciliation behavior live in the project file:

```json
{
  "version": 1,
  "inputs": {
    "left_format": "csv",
    "right_format": "json"
  },
  "report": {
    "format": "html"
  },
  "reconciliation": {
    "key_fields": ["invoice_id"],
    "compare_fields": ["amount", "settled_on", "customer_name"],
    "ignore_fields": ["exported_at"],
    "case_sensitive_keys": true,
    "field_normalizers": {
      "invoice_id": "integer"
    },
    "comparison_policies": {
      "amount": "decimal:0.01",
      "settled_on": "date",
      "customer_name": "text"
    },
    "validation_rules": {
      "status": [
        {"type": "required"},
        {"type": "allowed", "values": ["paid", "pending"]}
      ],
      "amount": [
        {"type": "numeric_range", "minimum": "0", "maximum": "10000"}
      ],
      "invoice_id": [
        {"type": "pattern", "pattern": "INV-[0-9]{3}"}
      ]
    }
  }
}
```

Run it with `--config`; `--key` is no longer required when `key_fields` is configured:

```powershell
reconforge compare accounting.export gateway.export --config reconforge.json
```

Configuration version `1` accepts only the documented keys. Unknown or duplicate JSON keys,
unsupported versions, wrong value types, duplicate field names, duplicate canonical validation
rules within a field, and invalid formats, normalizers, policies, or validation rules are rejected
before either dataset is read. `inputs.format` sets both input formats; `left_format` and
`right_format` can select them independently. `delimiter` is available for CSV inputs.

Command-line options have explicit precedence:

1. `--left-format` or `--right-format` overrides `--format`, which overrides the corresponding
   configured side format, which overrides configured `inputs.format`. Extension detection is the
   final fallback.
2. Supplying any `--key`, `--compare`, or `--ignore` replaces the corresponding configured list.
3. Repeated `--normalize` and `--policy` assignments merge with configured maps by field; a CLI
   assignment replaces the configured value for that same field.
4. `--delimiter`, `--ignore-key-case`, and `--case-sensitive-keys` override their configured
   scalar values.
5. `--report-format` overrides `report.format`; otherwise a recognized `--output` suffix selects
   the format. JSON remains the default.

The JSON report records the effective ignored fields and key-case behavior alongside normalizers
and comparison policies, so command-line overrides remain visible during review.

## Data validation

Validation rules are independent from value comparison. Configure one or more rules per field in
`reconciliation.validation_rules`; they run in listed order for every row on both inputs:

- `required` rejects null values. Other rule types skip null, so optional fields remain optional.
- `allowed` accepts only the exact strings in `values`.
- `numeric_range` parses finite decimal values and supports `minimum`, `maximum`,
  `minimum_inclusive`, and `maximum_inclusive`. Bounds are inclusive by default.
- `pattern` requires the entire value to match a Python regular expression.

Rules run after any configured field normalizer, while violations retain the original source
value. They may target ignored fields and are evaluated on unmatched rows, because data quality is
separate from record matching. A rule field must exist in both inputs.

Reports keep `validation_violations` separate from `differences`. Each violation includes the
input side, source, row number, field, original value, rule descriptor, and message. Summary fields
separately count `discrepancies`, `validation_violations`, and total `issues`. A validation-only run
uses status `validation_violations_found`; a run containing both uses
`differences_and_validation_violations_found`.

Rules for one field must have distinct canonical descriptors. This also rejects equivalent
numeric ranges written with different decimal spellings, preventing one logical rule from being
evaluated and reported twice.

The command exits with status `0` when both datasets reconcile, `1` when valid inputs contain
differences or validation violations, and `2` when an input or configuration error prevents
comparison.

## Batch reconciliation

Compare two directory trees recursively with the same reconciliation settings used by `compare`:

```powershell
reconforge batch accounting-exports gateway-exports `
  --config reconforge.json `
  --output batch-report.json
```

Recognized files are paired by their extension-free relative path, so `north/january.csv` can be
paired with `north/january.json`. Pair order is stable. Files present on only one side are listed
under `unmatched_inputs` and make the batch exit with status `1`. If one side contains multiple
recognized files with the same identity, such as `orders.csv` and `orders.json`, ReconForge stops
with status `2` and reports the ambiguity rather than choosing one.

For explicit pairing, use a versioned manifest whose paths are resolved relative to the manifest:

```json
{
  "version": 1,
  "pairs": [
    {
      "name": "north-january",
      "left": "accounting/north-2026-01.csv",
      "right": "gateway/north-2026-01.ndjson"
    }
  ]
}
```

Run it with the same configuration and override options:

```powershell
reconforge batch --manifest batch.json --config reconforge.json
```

Batch reports contain each normal single-pair report plus aggregate counts for clean pairs, pairs
with issues, matched records, discrepancies, validation violations, and unmatched inputs.

### Resumable batches

Use `--checkpoint` for a batch that may be interrupted or repeated:

```powershell
reconforge batch accounting-exports gateway-exports `
  --config reconforge.json `
  --checkpoint .reconforge/month-end.checkpoint.json
```

ReconForge writes the checkpoint atomically after every completed pair. On the next invocation it
reuses a stored result only when the pair name, resolved input identities, file contents, detected
or selected formats, reader options, and every effective reconciliation setting have the same
SHA-256 fingerprint. Changed pairs are recomputed independently; unchanged pairs are reused. An
interruption therefore leaves earlier completed pairs recoverable without treating partially
processed work as complete.

The batch report includes `execution.executed_pairs` and `execution.reused_pairs` whenever a
checkpoint is used. Invalid, corrupt, or unsupported checkpoint files stop the command with exit
status `2` instead of silently discarding recovery state.

The current checkpoint document version is `2`. Version `1` checkpoints cannot be resumed safely
with this release because fingerprint framing and checkpoint schema validation were hardened.
ReconForge does not auto-upgrade or silently trust them: archive or remove the old checkpoint,
then run the batch again to create a version `2` checkpoint from the current inputs. Version `2`
loading also rejects duplicate, missing, or unknown JSON members and noncanonical SHA-256
fingerprints before any stored pair result is reused.

## Local run history

Add `--history-db` to either execution command to persist its status, summary, configuration
metadata, and ordered input sources in a local SQLite database:

```powershell
reconforge compare accounting.csv gateway.csv `
  --config reconforge.json `
  --history-db .reconforge/history.sqlite

reconforge batch accounting-exports gateway-exports `
  --config reconforge.json `
  --history-db .reconforge/history.sqlite
```

Each invocation creates a distinct run, even when the inputs are repeated. A run and all of its
input rows are committed in one transaction, so a failed write cannot leave partial history.
Schema migrations also run transactionally and are tracked with SQLite's `user_version`; a store
created by a newer unsupported ReconForge version is rejected without modification.

List the newest runs or inspect one run by its integer ID:

```powershell
reconforge history list .reconforge/history.sqlite --limit 20
reconforge history show .reconforge/history.sqlite 42
```

History stores reconciliation metadata and summaries, not the complete difference payload. Keep
using `--output` when the full report needs to be retained. History is local-only and does not
require a service or network connection. The `history list` and `history show` commands require an
existing database; a misspelled or missing path returns status `2` and does not create an empty
store.

## Report formats

JSON is the canonical, lossless report model and remains the default. Choose a different exporter
with `--report-format`, a configured `report.format`, or a recognized `.csv`, `.html`, or `.htm`
output suffix:

```powershell
reconforge compare accounting.csv gateway.csv `
  --key invoice_id `
  --report-format csv `
  --output reconciliation.csv

reconforge batch accounting-exports gateway-exports `
  --config reconforge.json `
  --output batch-report.html
```

CSV contains a stable header followed by summary and issue rows for field differences, validation
violations, missing records, duplicate keys, schema-only fields, and unmatched batch inputs. Values
that spreadsheet programs could interpret as formulas are prefixed safely. HTML contains inline CSS
and no scripts, remote assets, timestamps, or network dependencies; all report values are escaped
and the canonical JSON remains available in a collapsible section. Both formats are deterministic
for the same canonical report.

Report files written by `compare`, `batch`, `inspect`, and `diff` are staged in a unique temporary
file beside the destination, flushed, and then atomically replaced. This avoids shared temporary
file collisions and prevents a failed write from leaving a partial report or replacing an existing
destination. Output filesystem failures produce a concise error and status `2` instead of an
uncaught traceback. Checkpoint files use the same collision-safe write path.

## Result investigation

Use `inspect` to query a saved single-result JSON report without re-running or modifying the
reconciliation:

```powershell
reconforge inspect reconciliation.json `
  --kind field_difference `
  --field amount `
  --key '["INV-100"]' `
  --row left:42 `
  --row-range right:40-60 `
  --side left `
  --sort key `
  --limit 50 `
  --output investigation.html
```

Repeat a filter option to match any supplied value within that dimension; different dimensions
are combined with AND. Exact keys are JSON arrays so composite keys and null components remain
unambiguous. Exact rows use `left:ROW` or `right:ROW`, while inclusive ranges use
`left:START-END` or `right:START-END`. `--side` selects issues that contain source context from
one side; schema-only issues have no source rows and therefore do not match a side or row filter.

The default `natural` order retains the report's stable issue order. Use `--sort kind`,
`--sort key`, `--sort field`, or `--sort source_row` for deterministic investigation-specific
ordering. The JSON page includes facet counts for kind, field, and source side across the full
filtered match set, independently of the page limit.

Continue from `page.next_cursor` with the same filters, sort mode, and limit:

```powershell
reconforge inspect reconciliation.json `
  --kind field_difference `
  --limit 50 `
  --cursor <NEXT_CURSOR>
```

Cursors are validated against the query that produced them, so malformed cursors or accidental
changes to filters, ordering, or page size fail explicitly. Offset pagination remains available
through `--offset` when a cursor is not supplied.

Investigation pages default to JSON on stdout. Select `--report-format csv` or
`--report-format html`, or use `--output`; `.json`, `.csv`, `.html`, and `.htm` suffixes select the
format automatically. CSV cells are protected against spreadsheet formula interpretation. HTML
is self-contained, script-free, escaped, and includes the canonical investigation data. File
output is replaced atomically so a failed write cannot leave a partially written destination.

The page reports both the original issue count and the filtered match count, plus `has_more` and
`next_offset`. Filtering and exporting never change the saved report, its reconciliation status,
or its original summary. Unknown issue kinds, fields not present in the result, wrong key arity,
invalid sides or rows, cursor mismatches, and out-of-range page bounds return exit status `2` with
an actionable error.

## Report-to-report deltas

Compare a current canonical JSON report with an earlier baseline without re-running either
reconciliation:

```powershell
reconforge diff baseline.json current.json `
  --status added `
  --status moved `
  --kind field_difference `
  --field amount `
  --limit 100 `
  --output delta.html
```

Delta entries are classified as `added`, `resolved`, or `unchanged` using stable issue
fingerprints. If the non-location content is identical but source row numbers changed, the entry
is classified as `moved` instead of being reported misleadingly as one resolved and one added
issue. Repeated status, kind, key, or field filters use OR within their dimension and AND across
dimensions. Pagination counts and facets are calculated over the complete filtered delta set.

Output defaults to deterministic JSON and can be rendered as analysis-ready CSV or self-contained
HTML with `--report-format` or an inferred `--output` suffix. File output is atomic. Use
`--fail-on-change` in CI to return status `1` when the filtered set contains any added, moved, or
resolved entry; successful execution with only unchanged entries returns `0`, and invalid input
returns `2`.

## Report verification

Treat saved single or batch JSON reports as untrusted input before investigation, automation, or
archival:

```powershell
reconforge verify reconciliation.json
reconforge verify batch-reconciliation.json
```

Verification rejects duplicate JSON keys, unknown or missing structural members, invalid recorded
normalizers, comparison policies and validation rules, wrong composite-key arity, contradictory or
duplicated issues, and summary or status values that do not match the report content. A valid
report prints deterministic JSON containing its report type, status, summary, and semantic SHA-256
fingerprint.

For a batch report, verification recursively applies every single-report invariant to each named
pair. It also rejects duplicate pair names or assigned sources, paired sources repeated as
unmatched, malformed checkpoint provenance, incomplete executed/reused pair coverage, and any
aggregate summary or status that cannot be derived from the nested results. Batch verification is
offline: archived input and checkpoint paths are treated as recorded identities and do not need to
exist. The verification response includes a fingerprint for every pair so a changed nested result
can be located directly.

Pin an expected result in an automated workflow:

```powershell
reconforge verify reconciliation.json `
  --expect-fingerprint sha256:<64-lowercase-hex-digits>
```

The fingerprint is calculated from the canonical report model, so JSON whitespace, object-key
order, a UTF-8 BOM, and batch pair or unmatched-input ordering do not change it. A semantic value
change does. Batch
`execution` metadata is intentionally excluded: executing a pair now or reusing the same result
from a trusted checkpoint produces the same semantic fingerprint. Verification returns `0` for a
valid report and matching expectation, `1` for a valid report whose fingerprint differs, and `2`
for malformed input or an invalid expectation. The command is read-only and never changes the
report.

## Input formats

CSV files use the first row as their header. JSON files must contain a top-level array of flat
objects, while NDJSON and JSONL files contain one flat object per non-blank line. JSON values may
be strings, numbers, booleans, or null; nested arrays and objects are rejected with their source
location. Every object in one input must contain the same fields, and duplicate object keys are
rejected instead of silently overwritten.

Formats are normally detected from `.csv`, `.json`, `.ndjson`, or `.jsonl`. Files with custom
extensions can be selected explicitly, including when the two sides use different formats:

```powershell
reconforge compare accounting.export gateway.export `
  --left-format csv `
  --right-format ndjson `
  --key invoice_id
```

Use `--format json` when both sides share one explicit format. `--delimiter` applies only to CSV
inputs; supplying it for JSON or NDJSON is treated as a configuration error.

## Development

The runtime uses only the Python standard library. The development extra pins the test and lint
tools used by the project. From the repository root, an editable development setup and the complete
local verification sequence are:

```powershell
python -m pip install -e ".[dev]"
python -m pip check
python -m ruff check .
python -m ruff format --check .
python -m compileall -q src tests
python -m pytest
```

The GitHub Actions workflow runs the same dependency, lint, formatting, compilation, and test
checks on Ubuntu with Python 3.11 and 3.12 for every pull request and every push to `main`. It uses
read-only repository permissions and pins the installer, build backend, development tools, and
action revisions to reduce toolchain drift.

## Design principles

ReconForge favors reproducibility over cleverness. Input normalization is explicit, comparison
order is stable, duplicate keys are surfaced instead of guessed through, and reports contain
enough source-row information to investigate every result. Network access is never required for
a reconciliation run.
