"""
gartman_connection.py

Reuses the SAME DSN + credentials your other OMPforecasting5 scripts
already connect with (see flooringwebapp.py's _connect()): a DSN named
"Gartman", with GARTMAN_UID / GARTMAN_PWD defined in OMP_secrets.py in
this same folder. Nothing to fill in here -- as long as this file sits
in the same folder as OMP_secrets.py, it just works.

If your setup differs (different DSN name, or you don't have
OMP_secrets.py), edit DSN_NAME below and/or set the GARTMAN_UID /
GARTMAN_PWD environment variables instead -- see the fallback at the
bottom of get_connection().
"""

import os
from pathlib import Path

import pyodbc

DSN_NAME = "Gartman"


def _load_secrets():
    """Same loading approach as flooringwebapp.py: exec OMP_secrets.py
    into an isolated namespace rather than importing it, so it works
    regardless of where this script is run from."""
    secrets_path = Path(__file__).resolve().parent / "OMP_secrets.py"
    if not secrets_path.exists():
        return "", ""
    namespace = {}
    with open(secrets_path, "r", encoding="utf-8") as f:
        exec(f.read(), namespace, namespace)
    uid = str(namespace.get("GARTMAN_UID", "")).strip()
    pwd = str(namespace.get("GARTMAN_PWD", "")).strip()
    return uid, pwd


def get_connection():
    uid, pwd = _load_secrets()

    # Fallback: environment variables, in case OMP_secrets.py isn't
    # present on whatever machine this runs on.
    if not uid or not pwd:
        uid = os.environ.get("GARTMAN_UID", "").strip()
        pwd = os.environ.get("GARTMAN_PWD", "").strip()

    if not uid or not pwd:
        raise RuntimeError(
            "Couldn't find Gartman credentials. Expected GARTMAN_UID / "
            "GARTMAN_PWD in OMP_secrets.py (same folder as this script) "
            "or as environment variables of the same names."
        )

    conn_str = f"DSN={DSN_NAME};UID={uid};PWD={pwd};"
    return pyodbc.connect(conn_str, autocommit=True, timeout=30)
