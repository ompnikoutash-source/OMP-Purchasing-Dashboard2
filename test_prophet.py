"""
Quick test to verify Prophet is installed and working correctly.
"""
import sys

print("Testing Prophet installation...")
print(f"Python: {sys.executable}")
print(f"Version: {sys.version}")
print()

# Test 1: Import
try:
    from prophet import Prophet
    print("[OK] Prophet imported successfully")
except ImportError as e:
    print(f"[FAIL] Failed to import Prophet: {e}")
    print("\nTo install Prophet, run:")
    print('  & "C:\\Users\\niko\\AppData\\Local\\Python\\bin\\python.exe" -m pip install prophet')
    sys.exit(1)

# Test 2: Create a simple model
import pandas as pd
import numpy as np

print("[OK] Creating test data...")

# Generate simple test data
dates = pd.date_range(start='2024-01-01', periods=52, freq='W')
values = 100 + np.random.randn(52) * 10 + np.sin(np.arange(52) * 2 * np.pi / 52) * 20

df = pd.DataFrame({
    'ds': dates,
    'y': values
})

print(f"[OK] Test data created: {len(df)} weeks of data")

# Test 3: Fit model
try:
    model = Prophet(
        yearly_seasonality=True,
        weekly_seasonality=False,
        daily_seasonality=False,
        seasonality_mode='multiplicative'
    )
    model.fit(df)
    print("[OK] Prophet model fitted successfully")
except Exception as e:
    print(f"[FAIL] Failed to fit model: {e}")
    sys.exit(1)

# Test 4: Make predictions
try:
    future = model.make_future_dataframe(periods=12, freq='W')
    forecast = model.predict(future)
    print(f"[OK] Forecast generated: {len(forecast)} periods")

    # Show sample output
    print("\nSample forecast (last 5 weeks):")
    print(forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']].tail())
except Exception as e:
    print(f"[FAIL] Failed to generate forecast: {e}")
    sys.exit(1)

print("\n" + "="*50)
print("SUCCESS! Prophet is installed and working correctly.")
print("="*50)
print("\nProphet will now be available as a forecasting method")
print("in your sundries, moulding, and flooring webapps.")
