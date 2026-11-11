# Domain Conventions & Financial Rules

* **Currency (CLP):** All amounts are signed integers representing Chilean Pesos (CLP). Decimals or cents are strictly prohibited for CLP amounts.
* **Negative Values (Credit Notes):** Negative numbers in `expense_items.amount` represent credit notes, discounts, refunds, or reversals.
* **Billing Periods (`billing_periods`):** Normalized strictly to the first day of each month: `'YYYY-MM-01'` (e.g. `'2025-07-01'`). Calendar year (`period_year`) and month (`period_month`) are stored in the master `billing_periods` table and linked via `period_date`.
* **Units Master Table (`units`):** Apartment dimension metadata (`tower`, `unit_number`, `alicuota`) is stored in the `units` table and linked via `unit_code` (e.g. `'B-504'`).
* **Chronological Ordering:** Order queries by `ORDER BY period_date ASC` or `ORDER BY p.period_year ASC, p.period_month ASC` (when joining `billing_periods p`).
