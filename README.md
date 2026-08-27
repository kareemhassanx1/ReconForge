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
- Persist run metadata and aggregate summaries in a transactional, versioned SQLite history.
- Reconcile rows using one or more key columns.
- Compare selected fields or all shared non-key fields.
- Detect duplicate keys without silently selecting an arbitrary record.
- Report fields that exist on only one side of the comparison.
- Write a stable, human-readable JSON report.
- Return a non-zero CLI status when differences are found.

## Quick start

Create two CSV files:

```csv
invoice_id,customer,amount,status
INV-100,Northwind,125.00,paid
INV-101,Contoso,80.00,pending
```

Then install the package and compare them:

```powershell
python -m pip install -e .
reconforge compare left.csv right.csv --key invoice_id --output report.json
```

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
unsupported versions, wrong value types, duplicate field names, and invalid formats, normalizers,
policies, or validation rules are rejected before either dataset is read. `inputs.format` sets
both input formats; `left_format` and `right_format` can select them independently. `delimiter` is
available for CSV inputs.

Command-line options have explicit precedence:

1. `--left-format` or `--right-format` overrides `--format`, which overrides the corresponding
   configured side format, which overrides configured `inputs.format`. Extension detection is the
   final fallback.
2. Supplying any `--key`, `--compare`, or `--ignore` replaces the corresponding configured list.
3. Repeated `--normalize` and `--policy` assignments merge with configured maps by field; a CLI
   assignment replaces the configured value for that same field.
4. `--delimiter`, `--ignore-key-case`, and `--case-sensitive-keys` override their configured
   scalar values.

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
require a service or network connection.

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
tools used by the project:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
```

## Design principles

ReconForge favors reproducibility over cleverness. Input normalization is explicit, comparison
order is stable, duplicate keys are surfaced instead of guessed through, and reports contain
enough source-row information to investigate every result. Network access is never required for
a reconciliation run.
