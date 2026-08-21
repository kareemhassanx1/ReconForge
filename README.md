# ReconForge

ReconForge is a local-first reconciliation tool for tabular business data. It compares two
exports using stable business keys and produces a deterministic report of missing records,
duplicate keys, schema drift, and field-level value differences.

The first working slice supports CSV inputs and JSON reports. The project is intentionally
designed around a small domain model so that additional readers, comparison policies, report
formats, and persistence can be added without coupling them to the command-line interface.

## Current capabilities

- Read UTF-8 CSV files with an explicit delimiter or safe delimiter detection.
- Normalize whitespace and empty values consistently at the input boundary.
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

The command exits with status `0` when both datasets reconcile, `1` when valid inputs contain
differences, and `2` when an input or configuration error prevents comparison.

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
