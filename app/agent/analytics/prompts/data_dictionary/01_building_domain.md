# Macro Building Expenses Domain ("Gastos Comunes del Edificio")

## 1. Dual Expense Classification (`operational` vs. `fondos`)
* **Gasto Común Operacional (`operational`)**: Ordinary operating expenses of the condominium (staff payroll, utilities, maintenance, repairs, insurance, supplies, etc.) corresponding to `TOTAL GASTOS COMUNES`.
* **Fondos y Recargos (`fondos`)**: Reserve funds and special surcharges (`Fondo de Reserva 5%`, etc.) added after `TOTAL GASTOS COMUNES`.
* **Total General Cobrado (`calculated_grand_total`)**: The sum of operational expenses plus reserve funds (`operational_total + fondos_total`), corresponding to final total billed.

## 2. Table Specifications (3NF Schema)

### Table: `billing_periods`
Master table for monthly accounting cycles.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `period_date` | VARCHAR(10) | PRIMARY KEY | Natural key formatted strictly as `'YYYY-MM-01'`. |
| `period_year` | INTEGER | INDEXED, NOT NULL | Four-digit calendar year (e.g. `2025`, `2026`). |
| `period_month` | INTEGER | INDEXED, NOT NULL | Calendar month number (1 to 12). |

### Table: `expense_reports`
Header table containing monthly building expense summaries.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Time-ordered UUIDv7 unique identifier. |
| `period_date` | VARCHAR(10) | UNIQUE, FK | References `billing_periods.period_date` (`'YYYY-MM-01'`). |
| `operational_total` | BIGINT | NOT NULL | Sum of operational expense sections (`TOTAL GASTOS COMUNES`) in CLP. |
| `fondos_total` | BIGINT | NOT NULL | Sum of reserve funds and special surcharges (`Fondos`) in CLP. |
| `calculated_grand_total` | BIGINT | NOT NULL | Final monthly total (`operational_total + fondos_total`) in CLP. |
| `created_at` | TIMESTAMP | NOT NULL | ISO 8601 creation timestamp. |

### Table: `expense_sections`
Categorical sections grouping related expense line items for a report.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Time-ordered UUIDv7 unique identifier. |
| `report_id` | UUID | FOREIGN KEY | References `expense_reports.id` (ON DELETE CASCADE). |
| `category_name` | VARCHAR(128) | NOT NULL | Normalized lowercase category name. |
| `expense_type` | VARCHAR(32) | NOT NULL | `'operational'` for operating sections or `'fondos'` for reserve/special funds. |
| `calculated_subtotal` | BIGINT | NOT NULL | Subtotal in Chilean Pesos (CLP) for this category. |
| `display_order` | INTEGER | DEFAULT 0 | Sequential presentation order in original statement. |

### Table: `expense_items`
Individual vendor invoices, line-item charges, and fund contributions within a section.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Time-ordered UUIDv7 unique identifier. |
| `section_id` | UUID | FOREIGN KEY | References `expense_sections.id` (ON DELETE CASCADE). |
| `name` | VARCHAR(255) | NOT NULL | Original line item description / invoice vendor text / fund name. |
| `normalized_name` | VARCHAR(255) | INDEXED | Normalized lowercase string without accents (preserving `'ñ'`). |
| `amount` | BIGINT | NOT NULL | Item cost in CLP. Negative for credit notes/discounts. |
| `display_order` | INTEGER | DEFAULT 0 | Sequential display order within the section. |

## 3. Canonical Categories
Filter case-insensitively: `LOWER(category_name) LIKE '%...%'`.
* `mantenciones y servicios` (Elevator, boiler, security cameras maintenance)
* `consumos` (Water, common gas 30%, electricity)
* `remuneraciones` (Staff salaries, social security, bonuses)
* `reparaciones y repuestos` (Emergency repairs, spare parts)
* `seguros` (Building insurance, fire protection)
* `insumos` (Cleaning supplies, pool chemicals, office stationery)
* `otros gastos` (Bank fees, miscellaneous petty cash)
* `asesorias y honorarios profesionales` (Legal, accounting, audit fees)
* `fondos` (`Fondo de Reserva 5%`, `Fondo Impermeabilizacion`, and any special funds)

## 4. Sample Queries
```sql
SELECT r.period_date, p.period_year, p.period_month, r.operational_total, r.fondos_total, r.calculated_grand_total
FROM expense_reports r
JOIN billing_periods p ON r.period_date = p.period_date
WHERE p.period_year = 2026
ORDER BY r.period_date ASC;
```
