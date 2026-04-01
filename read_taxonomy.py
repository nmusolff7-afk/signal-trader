import openpyxl

wb = openpyxl.load_workbook('APEX_TAXONOMY_EXPANDED.xlsx')
ws = wb.active

print("APEX_TAXONOMY_EXPANDED.xlsx - Event Taxonomy")
print("=" * 80)
print()

rows = list(ws.iter_rows(values_only=True))
headers = rows[0]
print(f"Columns: {headers}")
print()

print("Sample events (E001–E020):")
print()

for row in rows[1:21]:
    if not row[0]:  # Skip empty rows
        continue
    event_id = str(row[0])
    event_name = str(row[1]) if row[1] else ""
    keywords = str(row[2])[:60] if row[2] else ""
    
    print(f"{event_id:6s} | {event_name:35s}")
    print(f"       Keywords: {keywords}...")
    print()

print(f"Total events in file: {len([r for r in rows[1:] if r[0]])} events")
