# Backend Engineering, Security & Agent Architecture Guide

This document defines the architectural standards, secure coding practices, vulnerability prevention patterns, database migration procedures, and test verification gates for the **Building Analytics Backend** service (`nreyesh/backend`).

Any AI coding assistant modifying this service must strictly adhere to the guidelines below.

---

## 1. ⛔ Absolute Agent Guardrails (Never Violate)

1. **NO GIT COMMITS OR PUSHES:**
   * AI agents are **STRICTLY FORBIDDEN** from running `git commit`, `git push`, or modifying git history. Only the human engineer commits and pushes.
2. **ZERO HARDCODED SECRETS:**
   * Never hardcode API keys (e.g. `GEMINI_API_KEY`), database passwords, or service account credentials. All credentials must be sourced from environment variables or Google Cloud Secret Manager.
3. **NEVER CONNECT LOCALLY TO CLOUD SQL:**
   * Google Cloud SQL runs on a **100% private VPC network** with zero public IP addresses. Local development and testing must use the local SQLite sandbox (`condominium.db`) or a temporary local PostgreSQL Docker container.
4. **MANDATORY VERIFICATION BEFORE COMPLETION:**
   * Never report a feature or fix as finished without running the local test suite and maintaining the >=65% coverage requirement.

---

## 2. 🛡️ Secure Coding & Vulnerability Prevention Matrix

To eliminate security vulnerabilities, every code change must strictly comply with this matrix:

| Vulnerability / Threat | Risk Description | Mandatory Safe Pattern (Do This) | Anti-Pattern (Banned - Never Do) |
| :--- | :--- | :--- | :--- |
| **SQL Injection (SQLi)** | Attackers inject arbitrary SQL into queries | Use **SQLAlchemy 2.0 ORM** parameterized queries:<br>`stmt = select(UnitModel).where(UnitModel.unit_number == unit_num)`<br>If using Core `text()`, use bound parameters:<br>`text("SELECT * FROM units WHERE id = :id"), {"id": val}` | **BANNED:** Raw string interpolation or f-strings in queries:<br>`f"SELECT * FROM units WHERE id = '{val}'"`<br>`text(f"SELECT ...")` |
| **BOLA / IDOR** | Resident accesses another resident's private financial bills | Always enforce tenant and unit scope in query filters:<br>`select(UnitBill).where(UnitBill.unit_id == authorized_unit_id)` | **BANNED:** Querying solely by record ID without checking unit/tenant ownership. |
| **Insecure Deserialization** | Remote code execution via untrusted data | Parse incoming JSON and file payloads strictly with **Pydantic v2 schemas** (`model_validate_json`, etc.) | **BANNED:** Using `pickle.loads()`, `yaml.unsafe_load()`, `eval()`, or `exec()`. |
| **LLM Output Poisoning** | Malicious or malformed LLM outputs corrupt database | Pass LLM output through structured Pydantic schemas before database persistence. Validate bounds, types, and required fields. | **BANNED:** Directly writing raw LLM string output or unvalidated dictionaries into the database. |
| **PII & Credential Leakage** | Leaking auth tokens or financial records in logs | Sanitize logs. Use log filters or explicit string masking for tokens, passwords, and sensitive financial details. | **BANNED:** Printing entire auth headers, JWT tokens, or raw request payloads in `logger.info()` / `print()`. |
| **Resource Exhaustion (DoS)** | Unbounded queries crashing database or memory | Enforce pagination limits (`limit(100).offset(...)`) on all list endpoints. Enforce connection pooling limits in `config.py`. | **BANNED:** Unbounded `select(Model)` returning thousands of rows without pagination. |

### Safe Python Language Idioms
* **Explicit Type Annotations:** All function signatures and model fields must have explicit type hints (Python 3.11+ union syntax `str | None`).
* **Session Lifecycles:** Always manage database sessions using context managers:
  ```python
  with get_db() as session:
      # queries here
  ```
* **No Mutable Default Arguments:** Never use mutable defaults like `def parse_items(items=[])`; use `def parse_items(items: list[str] | None = None)`.

---

## 3. 🏗️ Architectural Invariants & Data Integrity

1. **Financial Precision:**
   * Chilean Pesos (CLP) have no fractional currency. All monetary amounts must be stored as exact integers (`BIGINT` or `INTEGER`). Never use floating-point types (`FLOAT`, `DOUBLE`) for financial balances.
   * Prorated shares (`alicuota`) must be stored as exact fixed-point `NUMERIC(8, 5)` to preserve sum-to-1.0 integrity across all 420 building units.
2. **Normalized 3NF Relational Hierarchy:**
   * Building Expenses (3-tier): `expense_reports` ➔ `expense_sections` ➔ `expense_items`.
   * Unit Billing Hierarchy: `unit_bills` ➔ `unit_bill_concepts`, `unit_bill_consumptions`, `unit_bill_charges`.
   * Foreign keys must enforce cascade rules as defined in `app/models.py`.

---

## 4. 🧪 Local Verification & Fast-Feedback Gates

Before completing any task, execute the test suite from the repository root:

```bash
# From repository root:
PYTHONPATH=backend pytest backend/tests/ -v --cov=backend/app --cov-report=term-missing --cov-fail-under=65

# Or from inside backend/:
cd backend && pytest tests/ -v
```

### Verification Gate Rules:
* All tests in `backend/tests/` **MUST** pass.
* Total test coverage **MUST NOT** drop below **65%** (configured in `pyproject.toml`).
* If you add a new model or route, you **MUST** add corresponding tests in `backend/tests/` to keep coverage above the threshold.

---

## 5. 🗄️ Database Migrations Protocol (Alembic)

Because Cloud SQL is 100% private, migrations are autogenerated locally using a temporary PostgreSQL Docker container:

```bash
# 1. Start local PostgreSQL container
docker run --name pg-local -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=condominium_db -p 5432:5432 -d postgres:15

# 2. Upgrade to current head
alembic upgrade head

# 3. Update backend/app/models.py with your changes

# 4. Generate migration script
alembic revision --autogenerate -m "describe_your_change"

# 5. Inspect and verify the generated script in backend/alembic/versions/

# 6. Stop and remove container
docker stop pg-local && docker rm pg-local
```

> **Review Rule:** Always inspect generated scripts. If a column was renamed, convert `op.drop_column` + `op.add_column` to `op.alter_column` to prevent data loss.

---

## 6. 🛠️ Offline Extraction Sandbox CLI

For offline feature testing, prompt engineering, or unit testing:
```bash
# Single unit extraction dry-run (does not write to DB)
PYTHONPATH=. python -m backend.app.extractor.cli --target units --unit B-504 --month 2026-09 --dry-run -v

# Building report test (writes to local SQLite condominium.db)
PYTHONPATH=. python -m backend.app.extractor.cli --target building --month 2026-09
```
* Suffix with `--dry-run` to test PDF extraction and LLM multimodal parsing without altering SQLite.
* Use `-v` / `--verbose` for detailed token usage and rate limiter logs.
