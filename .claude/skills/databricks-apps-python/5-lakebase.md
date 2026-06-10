# Lakebase (PostgreSQL) Connectivity

Lakebase provides low-latency transactional storage for Databricks Apps via a managed PostgreSQL interface.

**Docs**: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/lakebase

---

## When to Use Lakebase

| Use Case | Recommended Backend |
|----------|-------------------|
| Analytical queries on Delta tables | SQL Warehouse |
| Low-latency transactional CRUD | **Lakebase** |
| App-specific metadata/config | **Lakebase** |
| User session data | **Lakebase** |
| Large-scale data exploration | SQL Warehouse |

---

## Setup (on-demand, zero clicks)

Do NOT tell the user to click around the Databricks UI. Provision Lakebase from
the terminal **only when the app actually needs persistence**, then bind it
non-interactively:

1. Provision (or reuse) the lab's Lakebase instance:
   ```bash
   uv run python /app/python/source_code/scripts/lakebase_ensure.py
   ```
   This is idempotent (one instance per lab, reused across apps), waits until the
   instance is available, and writes the binding to `~/.coda/lakebase.json`. It
   prints the exact `databricks apps init --resource` flags to use. If it exits
   non-zero (e.g. the deploying identity lacks the database-create entitlement),
   the app can still be built without persistence — ask the user first.
2. Bind it when scaffolding (see the golden-path command in
   [SKILL.md](SKILL.md)). Once bound as an app resource, Databricks auto-injects
   the PostgreSQL connection env vars:

| Variable | Description |
|----------|-------------|
| `PGHOST` | Database hostname |
| `PGDATABASE` | Database name |
| `PGUSER` | PostgreSQL role (created per app) |
| `PGPASSWORD` | Role password |
| `PGPORT` | Port (typically 5432) |

3. Reference in `app.yaml`:

```yaml
env:
  - name: DB_CONNECTION_STRING
    valueFrom:
      resource: database
```

---

## Connection Patterns

### psycopg2 (Synchronous)

```python
import os
import psycopg2

conn = psycopg2.connect(
    host=os.getenv("PGHOST"),
    database=os.getenv("PGDATABASE"),
    user=os.getenv("PGUSER"),
    password=os.getenv("PGPASSWORD"),
    port=os.getenv("PGPORT", "5432"),
)

with conn.cursor() as cur:
    cur.execute("SELECT * FROM my_table LIMIT 10")
    rows = cur.fetchall()

conn.close()
```

### asyncpg (Asynchronous)

```python
import os
import asyncpg

async def get_data():
    conn = await asyncpg.connect(
        host=os.getenv("PGHOST"),
        database=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
        port=int(os.getenv("PGPORT", "5432")),
    )
    rows = await conn.fetch("SELECT * FROM my_table LIMIT 10")
    await conn.close()
    return rows
```

### SQLAlchemy

```python
import os
from sqlalchemy import create_engine

DATABASE_URL = (
    f"postgresql://{os.getenv('PGUSER')}:{os.getenv('PGPASSWORD')}"
    f"@{os.getenv('PGHOST')}:{os.getenv('PGPORT', '5432')}"
    f"/{os.getenv('PGDATABASE')}"
)

engine = create_engine(DATABASE_URL)
```

---

## Streamlit with Lakebase

```python
import streamlit as st
import psycopg2

@st.cache_resource
def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        database=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )
```

---

## Critical: requirements.txt

`psycopg2` and `asyncpg` are **NOT pre-installed** in the Databricks Apps runtime. You **MUST** include them in `requirements.txt` or the app will crash on startup:

```
psycopg2-binary
```

For async apps:
```
asyncpg
```

**This is the most common cause of Lakebase app failures.**

## Notes

- Lakebase is in **Public Preview**
- Each app gets its own PostgreSQL role with `Can connect and create` permission
- Lakebase is ideal alongside SQL warehouse: use Lakebase for app state, SQL warehouse for analytics
