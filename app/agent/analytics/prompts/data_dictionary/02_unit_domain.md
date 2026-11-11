# Unit-Level Apartment Expenses Domain ("Boleta de Cobro por Unidad")

## 1. Domain Structure (3NF Decoupled Schema)
Each unit billing statement (*"Nota de Cobro"*, e.g. Depto `B-504`) is linked to the master `units` table (`unit_code`) and `billing_periods` table (`period_date`), and contains three distinct billing components:
1. **Conceptos Departamento (`unit_bill_concepts`)**: Base prorated gasto común plus special funds (e.g. `Gasto Común`, `Fondo de Reserva 5%`, `Fondo Impermeabilizacion`). Subtotal stored in `unit_bills.calculated_dept_subtotal`.
2. **Consumos Generales (`unit_bill_consumptions`)**: Metered individual utility consumption, primarily central hot water (Torre B/A) with previous reading, current reading, consumption volume ($m^3$), unit rate ($\$/m^3$), and total. Subtotal stored in `unit_bills.calculated_consumo_total`.
3. **Gastos por Unidad (`unit_bill_charges`)**: Apartment-specific surcharges, fines, fixed fees (e.g. `Fondo de Indemnizacion`, parking fees). Subtotal stored in `unit_bills.calculated_unit_subtotal`.
4. **Total a Pagar del Mes (`calculated_total_month`)**: `calculated_dept_subtotal + calculated_consumo_total + calculated_unit_subtotal`.

## 2. Table Specifications

### Table: `units`
Master table for the 420 apartments in the condominium.
**CRITICAL**: `tower`, `unit_number`, and `alicuota` live in `units` (NOT in `unit_bills`). Always `JOIN units u ON b.unit_code = u.unit_code` when filtering or selecting `u.tower`, `u.unit_number`, or `u.alicuota`.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `unit_code` | VARCHAR(32) | PRIMARY KEY | Canonical composite code e.g. `'B-504'`, `'B-506'`. |
| `tower` | VARCHAR(16) | INDEXED, NOT NULL | Building tower identifier e.g. `'B'`, `'A'`. |
| `unit_number` | INTEGER | INDEXED, NOT NULL | Apartment number integer e.g. `504`, `506`. |
| `alicuota` | NUMERIC(8,5) | NULLABLE | Prorate percentage e.g. `0.16736` (meaning `0.16736%`). |
| `kastor_id` | INTEGER | UNIQUE, NULLABLE | External Kastor portal unit ID. |

### Table: `billing_periods`
Master table for monthly accounting cycles.
**CRITICAL**: `period_year` and `period_month` live in `billing_periods`. Always `JOIN billing_periods p ON b.period_date = p.period_date` when filtering or selecting `p.period_year` or `p.period_month`.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `period_date` | VARCHAR(10) | PRIMARY KEY | Period date formatted strictly as `'YYYY-MM-01'`. |
| `period_year` | INTEGER | NOT NULL | Four-digit calendar year (e.g. `2026`). |
| `period_month` | INTEGER | NOT NULL | Calendar month number (1 to 12). |

### Table: `unit_bills`
Header table containing monthly unit statement summaries.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Time-ordered UUIDv7 unique identifier. |
| `unit_code` | VARCHAR(32) | INDEXED, FK | References `units.unit_code` (e.g. `'B-504'`). |
| `period_date` | VARCHAR(10) | INDEXED, FK | References `billing_periods.period_date` (`'YYYY-MM-01'`). |
| `last_payment_date` | DATE | NULLABLE | ISO date `'YYYY-MM-DD'` of last payment. |
| `last_payment_amount` | BIGINT | NULLABLE | Last payment amount in CLP. |
| `calculated_dept_subtotal` | BIGINT | NOT NULL | **Subtotal Depto (Cuota Base + Fondos)** in CLP. |
| `calculated_consumo_total` | BIGINT | NOT NULL | **Agua Caliente** in CLP. |
| `calculated_unit_subtotal` | BIGINT | NOT NULL | **Cargos Particulares (Multas y otros)** in CLP. |
| `calculated_total_month` | BIGINT | NOT NULL | **Total Cobrado en el Mes** in CLP. |

### Table: `unit_bill_concepts`
Breakdown rows in "Tabla Conceptos" (Base prorating and individual fund quotas).

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Time-ordered UUIDv7 unique identifier. |
| `unit_bill_id` | UUID | FOREIGN KEY | References `unit_bills.id` (ON DELETE CASCADE). |
| `concept_name` | VARCHAR(128) | NOT NULL | Name e.g. `'Gasto Común'`, `'Fondo de Reserva 5%'`. |
| `normalized_name` | VARCHAR(128) | INDEXED, NOT NULL | Lowercase accent-stripped name. |
| `detail` | VARCHAR(64) | NULLABLE | Detail string e.g. `'0,16736 %'`, `'5,00 %'`. |
| `amount` | BIGINT | NOT NULL | Payable amount in CLP. |
| `display_order` | INTEGER | DEFAULT 0 | Sequential display order. |

### Table: `unit_bill_consumptions`
Breakdown rows in "Tabla Consumos Generales" (Metered hot water and utilities).

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Time-ordered UUIDv7 unique identifier. |
| `unit_bill_id` | UUID | FOREIGN KEY | References `unit_bills.id` (ON DELETE CASCADE). |
| `item_name` | VARCHAR(128) | NOT NULL | Item name e.g. `'Torre B'`, `'Agua Caliente'`. |
| `normalized_name` | VARCHAR(128) | INDEXED, NOT NULL | Lowercase accent-stripped name. |
| `previous_reading` | NUMERIC(10,3) | NOT NULL | Previous meter reading in $m^3$. |
| `current_reading` | NUMERIC(10,3) | NOT NULL | Current meter reading in $m^3$. |
| `consumption` | NUMERIC(10,3) | NOT NULL | Volume consumed in $m^3$ (`current - previous`). |
| `unit_rate` | NUMERIC(10,2) | NOT NULL | Tariff per $m^3$ in CLP (e.g. `8340.24`). |
| `total_amount` | BIGINT | NOT NULL | Total metered cost in CLP (`consumption * unit_rate`). |
| `display_order` | INTEGER | DEFAULT 0 | Sequential display order. |

### Table: `unit_bill_charges`
Breakdown rows in "Tabla Gastos por Unidad" (Fines, fixed charges, recargos).

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Time-ordered UUIDv7 unique identifier. |
| `unit_bill_id` | UUID | FOREIGN KEY | References `unit_bills.id` (ON DELETE CASCADE). |
| `charge_name` | VARCHAR(128) | NOT NULL | Charge description e.g. `'Fondo de Indemnizacion'`. |
| `normalized_name` | VARCHAR(128) | INDEXED, NOT NULL | Lowercase accent-stripped name. |
| `charge_type` | VARCHAR(32) | NOT NULL | Category e.g. `'fixed_charge'`, `'fine'`, `'recargo'`. |
| `amount` | BIGINT | NOT NULL | Surcharge amount in CLP. |
| `display_order` | INTEGER | DEFAULT 0 | Sequential display order. |

## 3. Sample Queries

### Unit Monthly Evolution (Mandatory 4 Reconciled Components)
```sql
SELECT 
    b.period_date, 
    b.calculated_dept_subtotal, 
    b.calculated_consumo_total, 
    b.calculated_unit_subtotal, 
    b.calculated_total_month
FROM unit_bills b
JOIN units u ON b.unit_code = u.unit_code
WHERE u.tower = 'B' AND u.unit_number = 504
ORDER BY b.period_date ASC;
```

### Hot Water Consumption and Tariff Evolution
```sql
SELECT b.period_date, c.consumption AS consumption_m3, c.unit_rate AS unit_rate_clp, c.total_amount AS total_clp
FROM unit_bill_consumptions c
JOIN unit_bills b ON c.unit_bill_id = b.id
JOIN units u ON b.unit_code = u.unit_code
WHERE u.tower = 'B' AND u.unit_number = 504
ORDER BY b.period_date ASC;
```

### Multi-Unit Comparison (Admin)
```sql
SELECT b.unit_code, u.tower, u.unit_number, b.period_date, b.calculated_total_month
FROM unit_bills b
JOIN units u ON b.unit_code = u.unit_code
WHERE (u.tower = 'B' AND u.unit_number IN (504, 506))
ORDER BY b.period_date ASC, b.unit_code ASC;
```
