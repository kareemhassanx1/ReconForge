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

The command exits with status `0` when both datasets reconcile, `1` when valid inputs contain
differences, and `2` when an input or configuration error prevents comparison.

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
