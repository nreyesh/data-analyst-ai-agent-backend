database:
  engine: postgresql_and_sqlite_compatible
  description: 3NF Condominium Financial Management Schema
  tables:
    units:
      primary_key: unit_code
      columns: [unit_code, tower, unit_number, alicuota, kastor_id]
    billing_periods:
      primary_key: period_date
      columns: [period_date, period_year, period_month]
    expense_reports:
      primary_key: id
      columns: [id, period_date, operational_total, fondos_total, calculated_grand_total]
    expense_sections:
      primary_key: id
      columns: [id, report_id, category_name, expense_type, calculated_subtotal, display_order]
    expense_items:
      primary_key: id
      columns: [id, section_id, name, normalized_name, amount, display_order]
    unit_bills:
      primary_key: id
      columns: [id, unit_code, period_date, last_payment_date, last_payment_amount, calculated_dept_subtotal, calculated_consumo_total, calculated_unit_subtotal, calculated_total_month]
    unit_bill_concepts:
      primary_key: id
      columns: [id, unit_bill_id, concept_name, normalized_name, detail, amount, display_order]
    unit_bill_consumptions:
      primary_key: id
      columns: [id, unit_bill_id, item_name, normalized_name, previous_reading, current_reading, consumption, unit_rate, total_amount, display_order]
    unit_bill_charges:
      primary_key: id
      columns: [id, unit_bill_id, charge_name, normalized_name, charge_type, amount, display_order]
