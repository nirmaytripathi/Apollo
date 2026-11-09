"""Copy accounts and saved runs from the local SQLite file into DATABASE_URL (e.g. Neon). Run once before deploying.

Usage:
  1. Put the Neon connection string in .env:  DATABASE_URL=postgresql://...?sslmode=require
  2. python backend/scripts/migrate_to_db.py

Reads LOCAL_DB (default output/apollo.db). Accounts keep their password hashes, so the same email and password
work afterwards; run owners are remapped to the new account ids. Rows already in the target are skipped, so it
is safe to run again.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, insert, select  # noqa: E402

from core import config  # noqa: E402
from core import db  # noqa: E402


def migrate(source_url: str) -> tuple[int, int]:
    """Copy users and runs from `source_url` into the configured database; returns (users seen, runs added)."""
    src = create_engine(source_url)
    ids: dict[str, str] = {}
    added = 0
    try:
        with src.connect() as s, db.engine().begin() as d:
            for u in s.execute(select(db.users)).mappings():
                found = d.execute(select(db.users.c.id).where(db.users.c.email == u["email"])).scalar()
                if found is None:
                    found = d.execute(insert(db.users).values(
                        email=u["email"], name=u["name"], password_hash=u["password_hash"], created_at=u["created_at"],
                    ).returning(db.users.c.id)).scalar_one()
                ids[str(u["id"])] = str(found)
            have = set(d.execute(select(db.runs.c.id)).scalars())
            for r in s.execute(select(db.runs)).mappings():
                if r["id"] in have:
                    continue
                row = dict(r)
                row["owner"] = ids.get(row["owner"], row["owner"]) if row["owner"] else None
                d.execute(insert(db.runs).values(**row))
                added += 1
    finally:
        src.dispose()
    return len(ids), added


def main() -> None:
    if not config.DATABASE_URL:
        raise SystemExit("Set DATABASE_URL in .env first (the Neon connection string).")
    local = Path(config.LOCAL_DB)
    if not local.exists():
        raise SystemExit(f"No local database at {local}; nothing to copy.")
    users, runs = migrate(f"sqlite:///{local}")
    print(f"{users} account(s) and {runs} new run(s) copied to {db.kind()}.")


if __name__ == "__main__":
    main()
