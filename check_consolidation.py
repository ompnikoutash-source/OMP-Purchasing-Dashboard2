import pandas as pd

# Read the Strip SKU Combination tab
try:
    df = pd.read_excel("StripSKUList.xlsx", sheet_name="Strip SKU Combination", header=0)
    print("Columns:", df.columns.tolist())
    print(f"Total rows: {len(df)}")
    
    # Look for GFALPH SKUs
    target_skus = ['GFALPH401L-10', 'GFALPH401R-10', 'GFALPH401-L', 'GFALPH401-R', 
                   'GFALPH403L-10', 'GFALPH403R-10', 'GFALPH403-L', 'GFALPH403-R',
                   'GFALPH405L-10', 'GFALPH405R-10', 'GFALPH405-L', 'GFALPH405-R']
    
    for col in df.columns:
        for sku in target_skus:
            if df[col].astype(str).str.upper().str.contains(sku.upper()).any():
                print(f"Found {sku} in column {col}")
                matching_rows = df[df[col].astype(str).str.upper().str.contains(sku.upper())]
                print(matching_rows)
                print()
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
