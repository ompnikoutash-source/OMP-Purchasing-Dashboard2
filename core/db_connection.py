"""
Shared database connection and credential management for OMP Forecasting applications.
"""

import os
import warnings
from pathlib import Path
from typing import Optional, Tuple

# Suppress pandas SQLAlchemy warning (we use pyodbc directly which still works fine)
warnings.filterwarnings(
    "ignore",
    message="pandas only supports SQLAlchemy connectable",
    category=UserWarning,
)

import pandas as pd
import pyodbc

# Default DSN name
DSN_NAME = "Gartman"


def _get_db_as_of_date(conn) -> pd.Timestamp:
    """Get the current date from the database server."""
    try:
        df = pd.read_sql("SELECT CURRENT DATE AS TODAY FROM SYSIBM.SYSDUMMY1", conn)
        if not df.empty and pd.notna(df.loc[0, "TODAY"]):
            return pd.Timestamp(df.loc[0, "TODAY"]).normalize()
    except Exception as e:
        print(f"  WARNING: Failed to read DB current date, using local clock. Error: {e}")
    return pd.Timestamp.now().normalize()


def _resolve_as_of_date(conn, as_of_date_override: str = "") -> pd.Timestamp:
    """
    Resolve the as-of date to use for calculations.

    Priority:
    1. Use override if provided and valid
    2. Otherwise use max of local date and DB date
    """
    if as_of_date_override:
        try:
            override = pd.Timestamp(as_of_date_override).normalize()
            print(f"  Using as-of date override: {override.date()}")
            return override
        except Exception as e:
            print(f"  WARNING: Invalid as_of_date_override='{as_of_date_override}', using fallback. Error: {e}")
    db_date = _get_db_as_of_date(conn)
    local_date = pd.Timestamp.now().normalize()
    chosen = max(db_date, local_date)
    print(f"  Local date: {local_date.date()}, DB date: {db_date.date()}")
    print(f"  Using as-of date: {chosen.date()} (max of local/DB)")
    return chosen


def _load_credentials(secrets_filename: str = "OMP_secrets.py") -> Tuple[str, str]:
    """
    Load database credentials from secrets file (preferred) or environment variables.

    Priority:
    1. Secrets file (OMP_secrets.py) - preferred for reliability
    2. Environment variables (GARTMAN_UID, GARTMAN_PWD) - fallback only

    Also detects and handles swapped/identical credentials by clearing env vars.
    """
    print("  Loading credentials...")

    # ALWAYS clear potentially corrupted environment variables first
    # This prevents issues where password gets copied into username field
    uid_env = os.getenv("GARTMAN_UID", "").strip()
    pwd_env = os.getenv("GARTMAN_PWD", "").strip()

    # If env vars are identical or appear corrupted, clear them immediately
    if uid_env and pwd_env and uid_env == pwd_env:
        print("  Clearing corrupted environment variables (UID == PWD)...")
        os.environ.pop("GARTMAN_UID", None)
        os.environ.pop("GARTMAN_PWD", None)
        uid_env = ""
        pwd_env = ""

    # Look for secrets file in the calling script's directory (authoritative source)
    secrets_path = Path(__file__).resolve().parent.parent / secrets_filename
    uid_file = ""
    pwd_file = ""

    if secrets_path.exists():
        namespace = {}
        with open(secrets_path, 'r', encoding='utf-8') as f:
            exec(f.read(), namespace, namespace)
        uid_file = str(namespace.get("GARTMAN_UID", "")).strip()
        pwd_file = str(namespace.get("GARTMAN_PWD", "")).strip()

    # Check if env vars are swapped compared to file - clear them if so
    if uid_env and pwd_env and uid_file and pwd_file:
        if uid_env == pwd_file and pwd_env == uid_file:
            print("  Clearing swapped environment variables...")
            os.environ.pop("GARTMAN_UID", None)
            os.environ.pop("GARTMAN_PWD", None)
            uid_env = ""
            pwd_env = ""

    # Decide which credentials to use - PREFER secrets file for reliability
    if uid_file and pwd_file:
        uid, pwd = uid_file, pwd_file
        print(f"  Loaded from secrets file: UID={uid}, PWD={'*' * len(pwd)}")
    elif uid_env and pwd_env:
        uid, pwd = uid_env, pwd_env
        print(f"  Using environment variables: UID={uid}, PWD={'*' * len(pwd)}")
    else:
        raise RuntimeError("Missing credentials")

    # Final validation: username and password should NOT be the same
    if uid == pwd:
        raise RuntimeError(f"ERROR: Username and password are identical ('{uid}'). This is incorrect!")

    return uid, pwd


def connect(dsn_name: str = DSN_NAME) -> pyodbc.Connection:
    """
    Establish a database connection using the configured DSN.

    Args:
        dsn_name: The ODBC DSN name to connect to

    Returns:
        An open pyodbc Connection object
    """
    uid, pwd = _load_credentials()
    conn_str = f"DSN={dsn_name};UID={uid};PWD={pwd};"
    print(f"  Attempting database connection...")
    try:
        conn = pyodbc.connect(conn_str, autocommit=True, timeout=30)
        print(f"  Database connection successful!")
        return conn
    except pyodbc.Error as e:
        print(f"  Database connection failed!")
        print(f"  Error: {e}")
        raise


def sql_escape(value: str) -> str:
    """Escape single quotes for SQL string literals."""
    return value.replace("'", "''")
