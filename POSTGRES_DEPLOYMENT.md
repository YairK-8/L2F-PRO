# PostgreSQL production setup

L2F keeps SQLite as a local-development fallback. Production should set both
`DATABASE_URL` and `REQUIRE_POSTGRES=1`; this prevents a missing environment
variable from silently starting a second, empty SQLite database.

## 1. Create the database

Use PostgreSQL 14 or newer. Prefer the pooled/PgBouncer connection URL supplied
by the database provider when one is available.

Copy the values from `.env.example` into the deployment environment. Do not
commit real credentials.

## 2. Copy the existing SQLite data

Stop application writes briefly, then run from the project directory:

```bash
DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DBNAME' \
python scripts/migrate_sqlite_to_postgres.py
```

The migration does not delete or modify `database/l2f.db`.

## 3. Start in PostgreSQL-only mode

Set these variables on every application instance:

```bash
DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DBNAME'
REQUIRE_POSTGRES=1
SECRET_KEY='a-stable-random-production-secret'
POSTGRES_POOL_MIN_CONN=1
POSTGRES_POOL_MAX_CONN=12
```

The total maximum pool size is `POSTGRES_POOL_MAX_CONN` multiplied by the
number of application processes. Keep that total below the provider's database
or pooler connection limit.

## 4. Verify before opening traffic

- Start the application and request `/health`.
- Log in to one branch and compare barcode/location counts with SQLite.
- Scan one morning item and one warehouse item.
- Restart the application and confirm the new data remains present.
- Keep the SQLite file as a rollback backup until production is verified.
