# Cross-Domain Mathematical & Schema Bridge (Unit vs. Building)

## 1. Relational Keys (3NF Schema)
Unit bills (`unit_bills`), units (`units`), billing periods (`billing_periods`), and building expense reports (`expense_reports`) are aligned by `period_date` and `unit_code`:
```sql
SELECT b.unit_code, u.alicuota, b.period_date, b.calculated_total_month, r.operational_total, r.calculated_grand_total
FROM unit_bills b
JOIN units u ON b.unit_code = u.unit_code
JOIN expense_reports r ON b.period_date = r.period_date
WHERE u.tower = 'B' AND u.unit_number = 504;
```

## 2. Prorating Relationships (Ley de Copropiedad 21.442)

1. **Base Gasto Común Prorating:**
   The `Gasto Común` line item in `unit_bill_concepts` is calculated by applying the unit's `units.alicuota` percentage to the building's `expense_reports.operational_total`:
   $$\text{Base Gasto Común} = \text{ROUND}\left( \text{expense\_reports.operational\_total} \times \frac{\text{units.alicuota}}{100} \right)$$
   *Example:* $\$55.709.656 \times (0.16736 / 100) = \$93.236 \text{ CLP}$ (exact match).

2. **Central Boiler Metered Tariff (`unit_rate`):**
   The hot water rate in `unit_bill_consumptions.unit_rate` (e.g. $\$8.340,24 / m^3$) is determined by dividing total central boiler fuel/gas expenses (in `expense_items` where `LOWER(normalized_name) LIKE '%gas%'` or `'caldera'`) by total community $m^3$ consumed. When boiler gas costs surge in winter, `unit_rate` increases even if the resident's personal consumption ($m^3$) remains constant.

3. **Special Funds Bridge:**
   Fund quotas in `unit_bill_concepts` (such as `Fondo Impermeabilizacion` or `Fondo de Reserva 5%`) correlate directly with sections in `expense_sections` where `expense_type = 'fondos'`.

## 3. Two-Part Causal Queries
When investigating why a unit bill increased:
- **Query 1 (Unit Delta):** Compares month-over-month differences in `calculated_dept_subtotal`, `calculated_consumo_total`, and `calculated_unit_subtotal` from `unit_bills b JOIN units u ON b.unit_code = u.unit_code WHERE b.unit_code = :target_unit`.
- **Query 2 (Building Drivers):** Compares building operational categories and fuel items between baseline and peak periods using `expense_sections` and `expense_reports`.
