# Database Schema Architecture: Condominium Financial Management

## 1. Overview & Architectural Goals

This database stores and links two core financial domains for a 420-apartment condominium in Chile:
1. **Building-Level Expenses (*Gastos Comunes del Edificio*)**: Total monthly operational costs, reserve funds (*fondos de reserva*), vendor invoices, and line-item categorization.
2. **Flat-Level Expenses (*Notas de Cobro por Unidad*)**: Individual billing for each of the 420 apartments, proportional allocations based on each unit's official *alícuota* percentage, metered utility consumption (e.g. hot water), and unit-level charges or fines.

### Target Engine & Stack
- **Database Engine**: PostgreSQL 15+ (Google Cloud SQL)
- **ORM / Migrations**: SQLAlchemy 2.0 + Alembic (`backend/app/models.py`, `backend/alembic/`)
- **Currency**: Chilean Peso (CLP) stored strictly as **`BIGINT` / `INTEGER`** (no decimals) to guarantee 100% financial precision.
- **Percentages**: Stored as exact **`NUMERIC(8, 5)`** (e.g., `0.16736%`).
- **Telemetry / Metadata**: PostgreSQL native **`JSONB`** for rich querying.

---

## 2. Entity-Relationship Diagram (ERD)

```mermaid
erDiagram
    BILLING_PERIODS ||--o{ EXPENSE_REPORTS : "anchors"
    BILLING_PERIODS ||--o{ UNIT_BILLS : "anchors"
    UNITS ||--o{ UNIT_BILLS : "receives bill"
    
    EXPENSE_REPORTS ||--o{ EXPENSE_SECTIONS : "categorizes into"
    EXPENSE_SECTIONS ||--o{ EXPENSE_ITEMS : "details line items"
    
    UNIT_BILLS ||--o{ UNIT_BILL_CONCEPTS : "itemizes charges"
    UNIT_BILLS ||--o{ UNIT_BILL_CONSUMPTIONS : "records meter readings"
    UNIT_BILLS ||--o{ UNIT_BILL_CHARGES : "records fines and fees"

    UNITS {
        varchar(32) unit_code PK "e.g. 'B-504'"
        varchar(16) tower "e.g. 'B'"
        int unit_number "e.g. 504"
        numeric alicuota "NUMERIC(8, 5) e.g. 0.16736"
        timestamp created_at
    }

    BILLING_PERIODS {
        varchar(10) period_date PK "Format: YYYY-MM-01"
        int period_year "e.g. 2026"
        int period_month "1 to 12"
        timestamp created_at
    }

    EXPENSE_REPORTS {
        uuid id PK "Native PostgreSQL UUID"
        varchar(10) period_date FK "References BILLING_PERIODS(period_date)"
        bigint operational_total "CLP (Total Gastos Comunes)"
        bigint fondos_total "CLP (Reserve funds)"
        bigint calculated_grand_total "CLP (Final total)"
        text source_file_path "Path/URL to original PDF (Cloud Storage)"
        jsonb telemetry_metadata "Native JSONB metadata"
        timestamp created_at
        timestamp updated_at
    }

    EXPENSE_SECTIONS {
        uuid id PK "Native PostgreSQL UUID"
        uuid report_id FK "References EXPENSE_REPORTS(id)"
        varchar(128) category_name "e.g. 'Mantenimiento'"
        varchar(32) expense_type "'operational' or 'fondos'"
        bigint calculated_subtotal "CLP Subtotal"
        int display_order
    }

    EXPENSE_ITEMS {
        uuid id PK "Native PostgreSQL UUID"
        uuid section_id FK "References EXPENSE_SECTIONS(id)"
        varchar(255) name "e.g. 'Mantencion Ascensores Otis'"
        varchar(255) normalized_name "Indexed lowercase name for trend queries"
        bigint amount "CLP Amount (can be negative for credit notes)"
        int display_order
    }

    UNIT_BILLS {
        uuid id PK "Native PostgreSQL UUID"
        varchar(32) unit_code FK "References UNITS(unit_code)"
        varchar(10) period_date FK "References BILLING_PERIODS(period_date)"
        date last_payment_date "Date of previous payment"
        bigint last_payment_amount "CLP Previous payment"
        bigint calculated_dept_subtotal "CLP Flat proportion"
        bigint calculated_consumo_total "CLP Metered utilities"
        bigint calculated_unit_subtotal "CLP Charges/fines"
        bigint calculated_total_month "CLP Total payable this month"
        text source_file_path "Path/URL to individual PDF (Cloud Storage)"
        jsonb telemetry_metadata "Native JSONB metadata"
        timestamp created_at
        timestamp updated_at
    }

    UNIT_BILL_CONCEPTS {
        uuid id PK "Native PostgreSQL UUID"
        uuid unit_bill_id FK "References UNIT_BILLS(id)"
        varchar(128) concept_name "e.g. 'Gasto Comun Ordinario'"
        varchar(128) normalized_name
        varchar(64) detail "e.g. '0,16736 %'"
        bigint amount "CLP Amount"
        int display_order
    }

    UNIT_BILL_CONSUMPTIONS {
        uuid id PK "Native PostgreSQL UUID"
        uuid unit_bill_id FK "References UNIT_BILLS(id)"
        varchar(128) item_name "e.g. 'Agua Caliente'"
        varchar(128) normalized_name
        numeric previous_reading "Meter previous reading"
        numeric current_reading "Meter current reading"
        numeric consumption "Units consumed (m3/kWh)"
        numeric unit_rate "Rate per unit in CLP"
        bigint total_amount "CLP Total for consumption"
        int display_order
    }

    UNIT_BILL_CHARGES {
        uuid id PK "Native PostgreSQL UUID"
        uuid unit_bill_id FK "References UNIT_BILLS(id)"
        varchar(128) charge_name "e.g. 'Multa ruidos molestos'"
        varchar(128) normalized_name
        varchar(32) charge_type "'recargo', 'fine', 'fixed_charge'"
        bigint amount "CLP Amount"
        int display_order
    }
```

---

## 3. Detailed Table Specifications

### 3.1. Dimension / Master Tables

#### `units` (The 420 apartments)
Represents the fixed physical apartments.
- **Columns**: `unit_code` (PK), `tower`, `unit_number`, `alicuota`, `created_at`
- **Constraints**:
  - `PRIMARY KEY (unit_code)` (e.g. `'B-504'`)
  - `UNIQUE (tower, unit_number)`
  - `CHECK (alicuota > 0)`

#### `billing_periods` (The time dimension)
Anchors monthly billing cycles cleanly.
- **Columns**: `period_date` (PK), `period_year`, `period_month`, `created_at`
- **Constraints**:
  - `PRIMARY KEY (period_date)` (format `'YYYY-MM-01'`)
  - `CHECK (period_month BETWEEN 1 AND 12)`

---

### 3.2. Building Expense Tables (Preserved 3-Tier Hierarchy)

#### `expense_reports`
The monthly general summary header for the condominium.
- `FOREIGN KEY (period_date) REFERENCES billing_periods(period_date) ON DELETE RESTRICT`.
- `UNIQUE (period_date)`: Only 1 building report per period.

#### `expense_sections`
Categorical grouping (Administration, Maintenance, Utilities, Reserve Funds).
- `FOREIGN KEY (report_id) REFERENCES expense_reports(id) ON DELETE CASCADE`.
- `UNIQUE (report_id, category_name)`.

#### `expense_items`
Individual line items and contractor invoices.
- `FOREIGN KEY (section_id) REFERENCES expense_sections(id) ON DELETE CASCADE`.
- `INDEX (normalized_name)` for historical vendor trend analysis.

---

### 3.3. Unit Billing Tables

#### `unit_bills`
The central monthly statement for an individual apartment (*Nota de Cobro*).
- `FOREIGN KEY (unit_code) REFERENCES units(unit_code) ON DELETE RESTRICT`.
- `FOREIGN KEY (period_date) REFERENCES billing_periods(period_date) ON DELETE RESTRICT`.
- `UNIQUE (unit_code, period_date)`: Guarantees no duplicate bills for the same unit in the same month.

#### `unit_bill_concepts`
Itemized prorated breakdown for the unit.
- `FOREIGN KEY (unit_bill_id) REFERENCES unit_bills(id) ON DELETE CASCADE`.

#### `unit_bill_consumptions`
Individual metered readings (e.g., hot water meters).
- `FOREIGN KEY (unit_bill_id) REFERENCES unit_bills(id) ON DELETE CASCADE`.

#### `unit_bill_charges`
Unit-specific fines, barbecue (quincho) fees, or late payment interest.
- `FOREIGN KEY (unit_bill_id) REFERENCES unit_bills(id) ON DELETE CASCADE`.
