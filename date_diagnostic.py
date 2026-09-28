import pandas as pd
import sundrieswebapp_refactored as m

conn = m.connect()
as_of = m._resolve_as_of_date(conn, m.AS_OF_DATE_OVERRIDE)
last_map = m.fetch_last_sale_dates_for_skus(conn, {"ADBOG621304G-"})
last_dt = last_map.get("ADBOG621304G-")
cutoff = as_of - pd.Timedelta(days=365)

print("as_of:", as_of)
print("cutoff:", cutoff)
print("last_dt:", last_dt)
print("active:", last_dt is not None and last_dt >= cutoff)

conn.close()
