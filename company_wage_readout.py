import csv
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from gartman_connection import get_connection


PERIODS = {
    "2025": (date(2025, 1, 1), date(2025, 12, 31)),
    "2026 YTD": (date(2026, 1, 1), date(2026, 8, 25)),
}


def as_date(value):
    return value.date() if isinstance(value, datetime) else value


def money(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


conn = get_connection()
cur = conn.cursor()
cur.execute('select "EMEMP#", EMNAME, EMHRSL, EMFREQ, EMRATE from GSFL2K.PREMPM')
master = {str(int(row[0])): row[1:] for row in cur.fetchall() if row[0] is not None}

cur.execute('select "RAEMP#", RADATE, RARATE from GSFL2K.PRRAISE order by "RAEMP#", RADATE')
rate_history = defaultdict(list)
for employee, effective, rate in cur.fetchall():
    rate_history[str(int(employee))].append((as_date(effective), Decimal(str(rate or 0))))

cur.execute(
    """
    select PTRUN#, PTEMP#, PTDATE, PTRGHR, PTOTHR
    from GSFL2K.PRTIMECD
    where PTDATE between ? and ?
    """,
    date(2025, 1, 1),
    date(2026, 8, 25),
)
timecards = defaultdict(list)
for run, employee, when, regular, overtime in cur.fetchall():
    timecards[str(int(employee))].append(
        (run, as_date(when), Decimal(str(regular or 0)), Decimal(str(overtime or 0)))
    )

rows = []
for employee, employee_timecards in timecards.items():
    if employee not in master:
        continue
    name, hourly_or_salary, frequency, base_rate = master[employee]
    name = " ".join(str(name).split())
    base_rate = Decimal(str(base_rate or 0))
    amounts = []

    for start, end in PERIODS.values():
        scoped = [row for row in employee_timecards if start <= row[1] <= end]
        if str(hourly_or_salary).strip().upper() == "S":
            runs = {run: when for run, when, regular, overtime in scoped}
            amount = Decimal("0")
            for when in runs.values():
                candidates = [
                    rate for effective, rate in rate_history[employee]
                    if effective <= when and rate >= 100
                ]
                amount += candidates[-1] if candidates else base_rate
            basis = "salary per payroll run"
        else:
            amount = Decimal("0")
            for run, when, regular, overtime in scoped:
                candidates = [
                    rate for effective, rate in rate_history[employee]
                    if effective <= when and 0 < rate < 100
                ]
                rate = candidates[-1] if candidates else base_rate
                amount += regular * rate + overtime * rate * Decimal("1.5")
            basis = "timecards x rate"
        amounts.append(amount)

    if amounts[0] or amounts[1]:
        rows.append((int(employee), name, amounts[0], amounts[1], basis))

rows.sort()
total_2025 = sum((row[2] for row in rows), Decimal("0"))
total_2026 = sum((row[3] for row in rows), Decimal("0"))

csv_path = Path(__file__).with_name("company_wage_readout.csv")
with csv_path.open("w", newline="", encoding="utf-8-sig") as output:
    writer = csv.writer(output)
    writer.writerow(["Employee Name", "Gartman #", "2025 Wages", "2026 YTD Wages", "Basis"])
    for employee, name, amount_2025, amount_2026, basis in rows:
        writer.writerow([name, employee, f"{money(amount_2025):.2f}", f"{money(amount_2026):.2f}", basis])
    writer.writerow([f"TOTAL ({len(rows)} employees)", "", f"{money(total_2025):.2f}", f"{money(total_2026):.2f}", ""])

print("Employee\tGartman #\t2025\t2026 YTD\tBasis")
for employee, name, amount_2025, amount_2026, basis in rows:
    print(f"{name}\t{employee}\t${money(amount_2025):,.2f}\t${money(amount_2026):,.2f}\t{basis}")
print(f"TOTAL ({len(rows)} employees)\t\t${money(total_2025):,.2f}\t${money(total_2026):,.2f}\t")
print(f"Employee master records: {len(master)}")
print(f"Employees with timecard activity: {len(timecards)}")
print(f"CSV created: {csv_path}")
conn.close()