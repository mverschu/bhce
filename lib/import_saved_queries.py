#!/usr/bin/env python3
"""Import lib/cq.json into BloodHound CE and set the category column.

BloodHound stores saved queries in Postgres. The import API keeps name, query,
and description, and leaves category empty, so the UI would file everything
under Uncategorized. After import, this updates saved_queries.category and
marks the rows public.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

BH_URL = os.environ.get("BH_URL", "http://bloodhound:8080").rstrip("/")
BH_CONFIG = Path(os.environ.get("BH_CONFIG", "/run/bh/bloodhound.config.json"))
CQ_PATH = Path(os.environ.get("CQ_PATH", "/queries/cq.json"))
READY_SECONDS = int(os.environ.get("BH_READY_SECONDS", "180"))


def log(message: str) -> None:
    print(message, flush=True)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def load_admin(path: Path) -> tuple[str, str]:
    config = json.loads(path.read_text())
    nested = config.get("default_admin")
    if isinstance(nested, dict):
        username = nested.get("principal_name") or "admin"
        password = nested.get("password") or ""
    else:
        username = config.get("default_admin.principal_name") or "admin"
        password = config.get("default_admin.password") or config.get("default_password") or ""
    if not password:
        raise SystemExit(f"Admin password not found in {path}")
    return str(username), str(password)


def load_queries(path: Path) -> list[dict]:
    document = json.loads(path.read_text())
    queries = []
    seen: set[str] = set()
    for raw in document["queries"]:
        name = (raw.get("name") or "").strip()
        category = (raw.get("category") or "").strip()
        steps = raw.get("queryList") or []
        step = next((item for item in steps if item.get("final")), steps[-1] if steps else None)
        cypher = ((step or {}).get("query") or "").strip()
        if not name or not category or not cypher:
            raise SystemExit(f"Query is missing a name, category, or Cypher: {name or raw}")
        if name in seen:
            raise SystemExit(f"Duplicate query name in {path}: {name}")
        seen.add(name)
        queries.append(
            {
                "name": name,
                "category": category,
                "query": cypher,
                "description": (raw.get("description") or "").strip(),
            }
        )
    if not queries:
        raise SystemExit(f"No queries in {path}")
    return queries


def request(method: str, url: str, token: str | None = None, data: bytes | None = None, content_type: str | None = None, timeout: int = 60) -> tuple[int, bytes]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def login(username: str, password: str) -> tuple[str, str]:
    payload = json.dumps({"login_method": "secret", "username": username, "secret": password}).encode()
    deadline = time.time() + READY_SECONDS
    url = f"{BH_URL}/api/v2/login"
    last = "BloodHound API did not become ready"
    while True:
        try:
            status, body = request("POST", url, data=payload, content_type="application/json", timeout=20)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = str(exc)
            status = 0
            body = b""
        if status in (200, 201):
            parsed = json.loads(body.decode() or "{}")
            data = parsed.get("data", parsed)
            if isinstance(data, str):
                data = json.loads(data)
            token = data.get("session_token") or ""
            user_id = data.get("user_id") or ""
            if not token or not user_id:
                raise SystemExit(f"Login response did not include a session: {body[:300]!r}")
            return token, user_id
        if status in (400, 401, 403):
            raise SystemExit(f"BloodHound rejected the admin login ({status}): {body[:300]!r}")
        if time.time() > deadline:
            raise SystemExit(f"{last} (last status {status})")
        time.sleep(3)


def existing_names(token: str) -> set[str]:
    names: set[str] = set()
    skip = 0
    limit = 1000
    while True:
        status, body = request(
            "GET",
            f"{BH_URL}/api/v2/saved-queries?skip={skip}&limit={limit}&scope=all",
            token=token,
            timeout=60,
        )
        if status != 200:
            raise SystemExit(f"Could not list saved queries ({status}): {body[:300]!r}")
        payload = json.loads(body.decode() or "{}")
        rows = payload.get("data") or []
        for row in rows:
            if row.get("name"):
                names.add(row["name"])
        count = int(payload.get("count") or 0)
        skip += len(rows)
        if not rows or skip >= count:
            return names


def build_zip(queries: list[dict]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, query in enumerate(queries, start=1):
            archive.writestr(
                f"{index:04d}.json",
                json.dumps(
                    {"name": query["name"], "query": query["query"], "description": query["description"]},
                    ensure_ascii=False,
                ).encode(),
            )
    return buffer.getvalue()


def import_missing(token: str, queries: list[dict]) -> None:
    present = existing_names(token)
    missing = [query for query in queries if query["name"] not in present]
    log(f"Saved queries already present: {len(queries) - len(missing)}; importing {len(missing)}")
    if not missing:
        return
    status, body = request(
        "POST",
        f"{BH_URL}/api/v2/saved-queries/import",
        token=token,
        data=build_zip(missing),
        content_type="application/zip",
        timeout=180,
    )
    if status not in (200, 201):
        raise SystemExit(f"Query import failed ({status}): {body[:500]!r}")
    log(body.decode(errors="replace").strip())


def ensure_psql() -> None:
    if shutil.which("psql"):
        return
    if not shutil.which("apk"):
        raise SystemExit("psql is not installed")
    errors = []
    for package in ("postgresql-client", "postgresql17-client", "postgresql16-client", "postgresql15-client"):
        result = subprocess.run(["apk", "add", "--no-cache", package], capture_output=True, text=True)
        if result.returncode == 0 and shutil.which("psql"):
            return
        errors.append(result.stderr.strip() or result.stdout.strip())
    raise SystemExit("Could not install psql:\n" + "\n".join(errors))


def write_sql(queries: list[dict], user_id: str, check_path: Path) -> str:
    values = ",\n".join(
        f"({sql_literal(query['name'])}, {sql_literal(query['category'])})" for query in queries
    )
    user = sql_literal(user_id)
    return f"""
CREATE TEMP TABLE bhce_queries (
    name text PRIMARY KEY,
    category text NOT NULL
);
INSERT INTO bhce_queries (name, category) VALUES
{values};
UPDATE saved_queries AS sq
SET category = incoming.category,
    updated_at = NOW()
FROM bhce_queries AS incoming
WHERE sq.name = incoming.name
  AND sq.user_id = {user};
INSERT INTO saved_queries_permissions (query_id, "public", created_at, updated_at)
SELECT sq.id, TRUE, NOW(), NOW()
FROM saved_queries AS sq
JOIN bhce_queries AS incoming ON incoming.name = sq.name
WHERE sq.user_id = {user}
  AND NOT EXISTS (
      SELECT 1
      FROM saved_queries_permissions AS permission
      WHERE permission.query_id = sq.id
          AND permission."public" IS TRUE
  );
\\pset tuples_only on
\\pset format unaligned
\\o {check_path}
SELECT COALESCE(json_agg(json_build_object(
    'name', incoming.name,
    'category', COALESCE(sq.category, ''),
    'public', EXISTS (
        SELECT 1
        FROM saved_queries_permissions AS permission
        WHERE permission.query_id = sq.id
          AND permission."public" IS TRUE
    )
)), '[]'::json)
FROM bhce_queries AS incoming
LEFT JOIN saved_queries AS sq
  ON sq.name = incoming.name
 AND sq.user_id = {user};
\\o
"""


def apply_categories(queries: list[dict], user_id: str) -> None:
    ensure_psql()
    check_path = Path("/tmp/bhce-query-check.json")
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as handle:
        handle.write(write_sql(queries, user_id, check_path))
        script = handle.name
    last_error = ""
    for attempt in range(1, 11):
        result = subprocess.run(
            ["psql", "-v", "ON_ERROR_STOP=1", "-X", "-q", "-f", script],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            break
        last_error = (result.stderr or result.stdout).strip()
        if "does not exist" in last_error and attempt < 10:
            log("Waiting for BloodHound to create saved query tables...")
            time.sleep(3)
            continue
        raise SystemExit(f"Category update failed: {last_error}")
    else:
        raise SystemExit(f"Category update failed: {last_error}")

    if not check_path.is_file():
        raise SystemExit(f"Category check did not write {check_path}")
    raw = check_path.read_text().strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end < start:
        raise SystemExit(f"Category check returned unexpected output: {raw[:300]!r}")
    rows = json.loads(raw[start : end + 1])
    found = {row["name"]: row for row in rows}
    problems = []
    for query in queries:
        row = found.get(query["name"])
        if row is None or row.get("category") != query["category"] or not row.get("public"):
            problems.append(query["name"])
    if problems:
        preview = ", ".join(problems[:8])
        raise SystemExit(f"{len(problems)} queries are missing a category or public flag: {preview}")
    log(f"Categorized {len(queries)} saved queries")


def main() -> None:
    queries = load_queries(CQ_PATH)
    username, password = load_admin(BH_CONFIG)
    log(f"Importing {len(queries)} custom queries as {username}")
    token, user_id = login(username, password)
    import_missing(token, queries)
    apply_categories(queries, user_id)


if __name__ == "__main__":
    main()
