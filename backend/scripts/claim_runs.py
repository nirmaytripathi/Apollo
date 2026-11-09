"""Give runs saved before accounts existed (no owner) to one account.

Such runs are hidden from every user, so one account never sees another's work. If they are yours,
assign them to your account:

Usage:
  python backend/scripts/claim_runs.py                      list how many runs have no owner
  python backend/scripts/claim_runs.py --email you@x.com    assign all of them to that account

Works on the configured database (DATABASE_URL, e.g. Neon, or the local SQLite file).
Restart the backend afterwards: it loads runs into memory at startup.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, update  # noqa: E402

from core import db  # noqa: E402


def unclaimed() -> int:
    with db.engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(db.runs).where(db.runs.c.owner.is_(None))).scalar_one()


def claim(email: str) -> int:
    """Assign every ownerless run to the account with this email; returns how many were assigned."""
    with db.engine().begin() as conn:
        user_id = conn.execute(select(db.users.c.id).where(db.users.c.email == email.strip().lower())).scalar()
        if user_id is None:
            raise SystemExit(f"No account with email {email!r}. Create it in the app first.")
        return conn.execute(update(db.runs).where(db.runs.c.owner.is_(None)).values(owner=str(user_id))).rowcount


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", help="account that should own the runs")
    args = parser.parse_args()
    if not args.email:
        print(f"{unclaimed()} run(s) have no owner. Pass --email to assign them to an account.")
        return
    n = claim(args.email)
    print(f"Assigned {n} run(s) to {args.email}. Restart the backend to see them.")


if __name__ == "__main__":
    main()
