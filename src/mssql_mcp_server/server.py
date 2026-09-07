import asyncio
import json
import logging
import os
import re
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
import pyodbc
from pyodbc import connect, Error
from mcp.server import Server
from mcp.types import Resource, Tool, TextContent
from pydantic import AnyUrl

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mssql_mcp_server")

# ODBC pooling ไม่ล้าง session option ตอน reuse connection — ถ้า SET SHOWPLAN_XML ค้าง
# query ถัดไปจะได้ plan แทนข้อมูลจริง กันด้วย _reset_plan_options(); ปิด pool ได้ด้วย env นี้
# (ปิดแล้วช้าขึ้นราว 270ms/call เพราะต้อง handshake ใหม่ทุกครั้ง)
if os.getenv("MSSQL_ODBC_POOLING", "true").strip().lower() in ("false", "0", "no", "off"):
    pyodbc.pooling = False

# pyodbc attribute สำหรับส่ง Azure AD access token ให้ ODBC Driver
SQL_COPT_SS_ACCESS_TOKEN = 1256

# ค่า MSSQL_AUTHENTICATION ที่ถือว่าเป็นการล็อกอินแบบ Azure AD (ใช้ az login)
_AAD_AUTH_VALUES = {"azure", "aad", "azcli", "cli", "token", "activedirectory"}


def _is_readonly() -> bool:
    """default read-only; ปิดได้ด้วย MSSQL_READONLY=false เท่านั้น"""
    return os.getenv("MSSQL_READONLY", "true").strip().lower() not in ("false", "0", "no", "off")


def _get_aad_token_struct() -> bytes:
    """ดึง Azure AD access token จาก az login แล้วแพ็คตามรูปแบบ SQL_COPT_SS_ACCESS_TOKEN"""
    token = None
    try:
        # ทางที่ดีสุดถ้ามี azure-identity: cache token ให้ + เร็วกว่า
        from azure.identity import AzureCliCredential
        token = AzureCliCredential().get_token("https://database.windows.net/.default").token
    except Exception:
        # fallback: เรียก az CLI ตรงๆ (ไม่ต้องลง dependency เพิ่ม)
        out = subprocess.check_output(
            ["az", "account", "get-access-token",
             "--resource", "https://database.windows.net/", "-o", "json"]
        )
        token = json.loads(out)["accessToken"]
    raw = token.encode("utf-16-le")
    return struct.pack(f"<I{len(raw)}s", len(raw), raw)


def get_db_settings():
    """อ่าน config จาก env; รองรับทั้ง SQL auth และ Azure AD (az login)"""
    auth = os.getenv("MSSQL_AUTHENTICATION", "").strip().lower()
    settings = {
        "driver": os.getenv("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server"),
        "server": os.getenv("MSSQL_HOST", "localhost"),
        "database": os.getenv("MSSQL_DATABASE"),
        "user": os.getenv("MSSQL_USER"),
        "password": os.getenv("MSSQL_PASSWORD"),
        "encrypt": os.getenv("MSSQL_ENCRYPT", "yes"),
        "trust_cert": os.getenv("TrustServerCertificate", "yes"),
        "trusted_connection": os.getenv("Trusted_Connection", "no"),
        "mode": "aad" if auth in _AAD_AUTH_VALUES else "sql",
    }

    if not settings["database"]:
        raise ValueError("Missing required config: MSSQL_DATABASE")
    if settings["mode"] == "sql" and not all([settings["user"], settings["password"]]):
        raise ValueError(
            "Missing required config for SQL auth: MSSQL_USER and MSSQL_PASSWORD "
            "(set MSSQL_AUTHENTICATION=azure to use az login instead)"
        )
    return settings


def _connection_string(s) -> str:
    base = (
        f"Driver={{{s['driver']}}};Server={s['server']};Database={s['database']};"
        f"Encrypt={s['encrypt']};TrustServerCertificate={s['trust_cert']};"
    )
    if s["mode"] == "aad":
        # AAD: ไม่มี UID/PWD — token ส่งผ่าน attrs_before ตอน connect
        return base
    return base + f"UID={s['user']};PWD={s['password']};Trusted_Connection={s['trusted_connection']};"


def get_connection():
    """เปิด connection ตาม auth mode; ตั้ง autocommit=False เพื่อให้ rollback ได้เสมอ (กัน write)"""
    s = get_db_settings()
    cs = _connection_string(s)
    if s["mode"] == "aad":
        conn = connect(cs, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: _get_aad_token_struct()}, timeout=30)
    else:
        conn = connect(cs, timeout=30)
    conn.autocommit = False
    return conn, s


# ---- Read-only guard -------------------------------------------------------

# statement เริ่มด้วยคำเหล่านี้ = เขียน/DDL/สั่งรัน → บล็อก
_WRITE_RE = re.compile(
    r"^(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|deny|"
    r"exec|execute|backup|restore|dbcc|bulk|use|sp_|xp_)\b",
    re.IGNORECASE,
)
# อนุญาตเฉพาะ read เท่านั้น
_ALLOWED_RE = re.compile(r"^(select|with|show\s+tables)\b", re.IGNORECASE)


def _strip_sql(query: str) -> str:
    """ตัด comment (/* */ และ --) และช่องว่างหัวท้าย เพื่อตรวจคำสั่งจริง"""
    q = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)
    q = re.sub(r"--[^\n]*", " ", q)
    return q.strip()


def assert_read_only(query: str) -> None:
    """โยน PermissionError ถ้า query ไม่ใช่ read-only (ชั้นป้องกันแรก)"""
    core = _strip_sql(query).rstrip(";").strip()
    if not core:
        raise ValueError("Empty query")
    # กัน stacked statements เช่น `SELECT 1; DELETE ...`
    if ";" in core:
        raise PermissionError("Multiple statements are not allowed in read-only mode")
    if _WRITE_RE.match(core):
        raise PermissionError(
            f"Write/DDL statements are blocked in read-only mode: '{core.split()[0].upper()}'"
        )
    if not _ALLOWED_RE.match(core):
        raise PermissionError("Only SELECT / WITH queries are allowed in read-only mode")
    # กัน SELECT ... INTO (สร้างตารางใหม่)
    if core.lower().startswith("select") and re.search(r"\binto\b", core, re.IGNORECASE):
        raise PermissionError("SELECT ... INTO is blocked in read-only mode")


# ---- Execution plan capture (SHOWPLAN_XML / STATISTICS XML) ----------------

SHOWPLAN_NS = "{http://schemas.microsoft.com/sqlserver/2004/07/showplan}"

_SCAN_OPS = {"Table Scan", "Clustered Index Scan", "Index Scan",
             "Remote Index Scan", "Remote Scan", "Columnstore Index Scan"}

# estimated mode: SHOWPLAN_XML ON ทำให้ statement ไม่ถูกรันจริง จึงเปิดกว้างกว่า execute_sql ได้
_EXPLAIN_READ_RE = re.compile(r"^(select|with)\b", re.IGNORECASE)
_EXPLAIN_EXEC_RE = re.compile(r"^(exec|execute|insert|update|delete|merge)\b", re.IGNORECASE)
_EXPLAIN_BLOCK_RE = re.compile(
    r"^(drop|alter|create|truncate|grant|revoke|deny|backup|restore|dbcc|bulk|use|sp_|xp_)\b",
    re.IGNORECASE,
)


def _env_flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() not in ("false", "0", "no", "off")


def assert_explain_safe(query: str, mode: str) -> None:
    """guard สำหรับ explain_query — estimated ผ่อนได้เพราะ SHOWPLAN ไม่รันจริง, actual รันจริงจึงเข้มกว่า"""
    core = _strip_sql(query).rstrip(";").strip()
    if not core:
        raise ValueError("Empty query")
    if ";" in core:
        raise PermissionError("Multiple statements are not allowed")
    if _EXPLAIN_BLOCK_RE.match(core):
        raise PermissionError(f"Blocked statement for plan capture: '{core.split()[0].upper()}'")

    if _EXPLAIN_READ_RE.match(core):
        if mode == "actual" and core.lower().startswith("select") and re.search(r"\binto\b", core, re.IGNORECASE):
            raise PermissionError("SELECT ... INTO is blocked in mode='actual'")
        return

    if not _EXPLAIN_EXEC_RE.match(core):
        raise PermissionError("Only SELECT / WITH / EXEC / INSERT / UPDATE / DELETE / MERGE can be explained")

    if mode == "estimated":
        if not _env_flag("MSSQL_EXPLAIN_ALLOW_EXEC", True):
            raise PermissionError("EXEC/DML plans are disabled (MSSQL_EXPLAIN_ALLOW_EXEC=false)")
    elif not _env_flag("MSSQL_EXPLAIN_ALLOW_ACTUAL_EXEC", False):
        raise PermissionError(
            "mode='actual' really executes the statement, so EXEC/DML is disabled by default. "
            "Use mode='estimated', or set MSSQL_EXPLAIN_ALLOW_ACTUAL_EXEC=true "
            "(the connection still rolls back, but side effects outside the transaction are not undone)."
        )


def _is_plan_cursor(cursor) -> bool:
    d = cursor.description
    return bool(d) and len(d) == 1 and "Showplan" in str(d[0][0])


def _next_all(cursor) -> None:
    try:
        while cursor.nextset():
            pass
    except Error:
        pass


def _drain(cursor, max_rows: int):
    """เก็บ plan XML + ตัวอย่างแถว; ต้องดูด result set ให้หมด actual counters ถึงจะครบ

    pyodbc ล้าง cursor.messages ทิ้งทุกครั้งที่ nextset() จึงต้องเก็บสะสมระหว่างทาง
    """
    plans, datasets, messages = [], [], []

    def sweep():
        for m in cursor.messages or []:
            messages.append(str(m[1]))

    while True:
        if cursor.description is not None:
            if _is_plan_cursor(cursor):
                plans.extend(str(r[0]) for r in cursor.fetchall() if r[0])
            else:
                cols = [d[0] for d in cursor.description]
                rows = cursor.fetchmany(max_rows) if max_rows > 0 else []
                extra = 0
                while True:
                    chunk = cursor.fetchmany(2000)
                    if not chunk:
                        break
                    extra += len(chunk)
                datasets.append((cols, rows, extra))
        sweep()
        try:
            has_next = cursor.nextset()
        except Error:
            break
        sweep()
        if not has_next:
            break
    return plans, datasets, messages


_PLAN_RESET = (
    "SET SHOWPLAN_XML OFF",
    "SET STATISTICS XML OFF",
    "SET STATISTICS IO OFF",
    "SET STATISTICS TIME OFF",
)


def _reset_plan_options(conn) -> None:
    """ปิด SET option ทุกตัวก่อนคืน connection — กัน session state รั่วไปหา query ถัดไป"""
    try:
        cur = conn.cursor()
        for stmt in _PLAN_RESET:
            cur.execute(stmt)
            _next_all(cur)
    except Exception as e:
        logger.warning(f"Could not reset plan SET options: {e}")


def _run_explain(query: str, mode: str, max_rows: int):
    conn, s = get_connection()
    try:
        cur = conn.cursor()
        if mode == "estimated":
            cur.execute("SET SHOWPLAN_XML ON")
            # canary: ถ้า SHOWPLAN ไม่ติด SELECT 1 จะคืนค่า 1 แทน plan → ห้ามรัน query ต่อ
            cur.execute("SELECT 1 AS mcp_showplan_canary")
            engaged = _is_plan_cursor(cur)
            cur.fetchall()
            _next_all(cur)
            if not engaged:
                raise PermissionError(
                    "SET SHOWPLAN_XML ON did not engage — aborted before running the query."
                )
            cur.execute(query)
            plans, datasets, messages = _drain(cur, 0)
        else:
            cur.execute("SET STATISTICS XML ON")
            cur.execute("SET STATISTICS IO ON")
            cur.execute("SET STATISTICS TIME ON")
            cur.execute(query)
            plans, datasets, messages = _drain(cur, max_rows)
        return plans, datasets, messages, s
    finally:
        _reset_plan_options(conn)
        # rollback เสมอ เหมือน execute_sql — server นี้ไม่ commit อะไรทั้งสิ้น
        conn.rollback()
        conn.close()


# ---- Plan XML → summary ----------------------------------------------------

def _strip_brackets(v: str) -> str:
    return (v or "").replace("[", "").replace("]", "")


def _plan_summary(plan_xml: str, actual: bool) -> list[str]:
    ns = SHOWPLAN_NS
    try:
        root = ET.fromstring(plan_xml)
    except ET.ParseError as e:
        return [f"  (could not parse plan XML: {e})"]

    parent = {c: p for p in root.iter() for c in p}

    def nearest_relop(e):
        a = parent.get(e)
        while a is not None and a.tag != ns + "RelOp":
            a = parent.get(a)
        return a

    kids: dict = {}
    for e in root.iter(ns + "RelOp"):
        kids.setdefault(nearest_relop(e), []).append(e)

    def own_cost(e) -> float:
        c = float(e.get("EstimatedTotalSubtreeCost") or 0)
        for k in kids.get(e, []):
            c -= float(k.get("EstimatedTotalSubtreeCost") or 0)
        return c

    def obj_name(e) -> str:
        for o in e.iter(ns + "Object"):
            if nearest_relop(o) is e:
                t = _strip_brackets(o.get("Table"))
                i = _strip_brackets(o.get("Index"))
                return f"{t}.{i}" if i else t
        return ""

    def runtime(e):
        rti = e.find(ns + "RunTimeInformation")
        if rti is None:
            return None, None
        rows = sum(int(t.get("ActualRows") or 0) for t in rti)
        execs = max((int(t.get("ActualExecutions") or 0) for t in rti), default=0)
        return rows, execs

    lines: list[str] = []
    stmts = [e for e in root.iter() if e.tag.startswith(ns + "Stmt")]
    for stmt in stmts:
        qp = stmt.find(ns + "QueryPlan")
        if qp is None:
            continue
        text = " ".join((stmt.get("StatementText") or "").split())
        lines.append("")
        lines.append(f"── {text[:200]}{'…' if len(text) > 200 else ''}")

        meta = [f"subtree cost={stmt.get('StatementSubTreeCost')}"]
        if qp.get("DegreeOfParallelism"):
            meta.append(f"DOP={qp.get('DegreeOfParallelism')}")
        if qp.get("CachedPlanSize"):
            meta.append(f"cached plan={qp.get('CachedPlanSize')}KB")
        if qp.get("CompileTime"):
            meta.append(f"compile={qp.get('CompileTime')}ms/{qp.get('CompileCPU')}ms cpu")
        mg = qp.find(ns + "MemoryGrantInfo")
        if mg is not None and mg.get("SerialDesiredMemory"):
            meta.append(f"desired grant={mg.get('SerialDesiredMemory')}KB")
        if stmt.get("StatementOptmEarlyAbortReason"):
            meta.append(f"optm abort={stmt.get('StatementOptmEarlyAbortReason')}")
        lines.append("   " + ", ".join(meta))

        for mig in qp.iter(ns + "MissingIndexGroup"):
            for mi in mig.iter(ns + "MissingIndex"):
                tbl = f"{_strip_brackets(mi.get('Schema'))}.{_strip_brackets(mi.get('Table'))}"
                parts = []
                for cg in mi.iter(ns + "ColumnGroup"):
                    cols = ", ".join(_strip_brackets(c.get("Name")) for c in cg.iter(ns + "Column"))
                    parts.append(f"{cg.get('Usage')}: {cols}")
                lines.append(f"   ⚠ missing index (impact {mig.get('Impact')}) on {tbl} — {' | '.join(parts)}")

        for w in qp.iter(ns + "Warnings"):
            for k, v in w.attrib.items():
                if str(v).lower() in ("true", "1"):
                    lines.append(f"   ⚠ warning: {k}")
            for c in w:
                tag = c.tag[len(ns):]
                if tag == "PlanAffectingConvert":
                    lines.append(f"   ⚠ implicit convert ({c.get('ConvertIssue')}): {c.get('Expression')}")
                elif tag == "SpillToTempDb":
                    lines.append(f"   ⚠ spill to tempdb (level {c.get('SpillLevel')})")
                elif tag == "ColumnsWithNoStatistics":
                    cols = ", ".join(
                        f"{_strip_brackets(x.get('Table'))}.{_strip_brackets(x.get('Column'))}"
                        for x in c.iter(ns + "ColumnReference")
                    )
                    lines.append(f"   ⚠ no statistics: {cols}")
                else:
                    lines.append(f"   ⚠ warning: {tag}")

        relops = [e for e in stmt.iter(ns + "RelOp")]
        top = sorted(relops, key=own_cost, reverse=True)[:8]
        if top:
            lines.append("   top operators (by own cost):")
        for e in top:
            oc = own_cost(e)
            if oc <= 0 and e.get("EstimatedTotalSubtreeCost") in (None, "0"):
                continue
            est = e.get("EstimateRows")
            bits = [f"{oc:.4f}", e.get("PhysicalOp") or "?"]
            on = obj_name(e)
            if on:
                bits.append(on)
            bits.append(f"est rows={est}")
            if actual:
                ar, ex = runtime(e)
                if ar is not None:
                    bits.append(f"actual rows={ar}")
                    if ex and ex > 1:
                        bits.append(f"execs={ex}")
                    try:
                        f_est = float(est or 0) * (ex or 1)
                        if f_est > 0 and (ar / f_est > 10 or f_est / max(ar, 1) > 10):
                            bits.append("‼ estimate skew")
                    except (TypeError, ValueError):
                        pass
            if (e.get("PhysicalOp") or "") in _SCAN_OPS:
                bits.append("‼ scan")
            lines.append("     " + "  ".join(bits))
    return lines


_IO_RE = re.compile(r"Table '(?P<t>[^']+)'\.\s*Scan count (?P<sc>\d+), logical reads (?P<lr>\d+)")
_IO_LOB_RE = re.compile(r"lob logical reads (\d+)")
_TIME_RE = re.compile(r"CPU time = (\d+) ms,\s*elapsed time = (\d+) ms")


def _stats_summary(messages: list[str]) -> list[str]:
    io: dict[str, list[int]] = {}
    compile_t, exec_t = [], []
    seen = set()
    for m in messages:
        if m in seen:  # sweep() ซ้ำ message เดิมได้ ต้องกันไว้
            continue
        seen.add(m)
        for mt in _IO_RE.finditer(m):
            lob = _IO_LOB_RE.search(m[mt.end():])
            agg = io.setdefault(mt.group("t").replace("[", "").replace("]", ""), [0, 0, 0])
            agg[0] += int(mt.group("sc"))
            agg[1] += int(mt.group("lr"))
            agg[2] += int(lob.group(1)) if lob else 0
        bucket = compile_t if "parse and compile" in m else exec_t
        bucket.extend((int(a), int(b)) for a, b in _TIME_RE.findall(m))

    lines: list[str] = []
    if exec_t or compile_t:
        parts = []
        if exec_t:
            parts.append(f"execution CPU {sum(t[0] for t in exec_t)} ms, elapsed {sum(t[1] for t in exec_t)} ms")
        if compile_t:
            parts.append(f"parse/compile CPU {sum(t[0] for t in compile_t)} ms, elapsed {sum(t[1] for t in compile_t)} ms")
        lines.append("STATISTICS TIME: " + "; ".join(parts))
    if io:
        lines.append("STATISTICS IO (top by logical reads):")
        for t, (sc, lr, lob) in sorted(io.items(), key=lambda kv: -kv[1][1])[:15]:
            lines.append(f"   {lr:>10,} reads  scans={sc:<5} {'lob=' + str(lob) + '  ' if lob else ''}{t}")
        lines.append(f"   {sum(v[1] for v in io.values()):>10,} reads  TOTAL")
    return lines


def _plan_dir() -> Path:
    d = os.getenv("MSSQL_PLAN_DIR") or os.path.join(tempfile.gettempdir(), "mssql_mcp_plans")
    p = Path(d).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save_plan(xml: str, idx: int, db: str) -> str:
    safe_db = re.sub(r"[^A-Za-z0-9_.-]", "_", db or "db")
    f = _plan_dir() / f"{safe_db}_{datetime.now():%Y%m%d_%H%M%S_%f}_{idx}.sqlplan"
    f.write_text(xml, encoding="utf-8")
    return str(f)


async def _explain_query(arguments: dict) -> list[TextContent]:
    query = arguments.get("query")
    if not query:
        raise ValueError("Query is required")
    mode = (arguments.get("mode") or "estimated").strip().lower()
    if mode not in ("estimated", "actual"):
        raise ValueError("mode must be 'estimated' or 'actual'")
    include_xml = bool(arguments.get("include_xml"))
    max_rows = int(arguments.get("max_rows") or 0)

    try:
        assert_explain_safe(query, mode)
    except (PermissionError, ValueError) as e:
        logger.warning(f"Blocked explain_query: {e}")
        return [TextContent(type="text", text=f"Blocked: {e}")]

    try:
        plans, datasets, messages, s = _run_explain(query, mode, max_rows)
    except PermissionError as e:
        return [TextContent(type="text", text=f"Blocked: {e}")]
    except Error as e:
        msg = str(e)
        if "262" in msg or "SHOWPLAN" in msg.upper():
            msg += ("\nHint: this login needs the SHOWPLAN permission on the database "
                    "(GRANT SHOWPLAN TO [<user>]). Plan capture works on the UAT connection.")
        logger.error(f"explain_query failed: {e}")
        return [TextContent(type="text", text=f"Error capturing plan: {msg}")]
    except Exception as e:
        logger.error(f"explain_query failed: {e}")
        return [TextContent(type="text", text=f"Error capturing plan: {e}")]

    actual = mode == "actual"
    out = [f"Execution plan ({mode}) — {s['server']}/{s['database']}"]
    if not plans:
        out.append("No plan XML returned. (Estimated plans for a proc that builds #temp tables "
                   "can fail to compile — try mode='actual', or explain the inner statement.)")

    for i, xml in enumerate(plans, 1):
        path = _save_plan(xml, i, s["database"])
        out.append("")
        out.append(f"[plan {i}/{len(plans)}] full XML saved: {path} ({len(xml):,} chars)")
        out.extend(_plan_summary(xml, actual))

    if messages:
        stats = _stats_summary(messages)
        if stats:
            out.append("")
            out.extend(stats)

    for cols, rows, extra in datasets:
        out.append("")
        out.append(f"Result set: {len(cols)} cols, {len(rows) + extra} rows returned"
                   f"{f' (showing {len(rows)})' if rows else ' (data not returned)'}")
        if rows:
            out.append(",".join(cols))
            out.extend(",".join(map(str, r)) for r in rows)

    if include_xml:
        cap = int(os.getenv("MSSQL_PLAN_INLINE_CHARS", "200000"))
        for i, xml in enumerate(plans, 1):
            out.append("")
            out.append(f"--- plan {i} XML ---")
            out.append(xml[:cap] + ("\n… truncated, read the saved file for the rest" if len(xml) > cap else ""))

    return [TextContent(type="text", text="\n".join(out))]


# Initialize server
app = Server("mssql_mcp_server")


@app.list_resources()
async def list_resources() -> list[Resource]:
    """List MSSQL tables as resources."""
    try:
        conn, _ = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE';")
                tables = cursor.fetchall()
                logger.info(f"Found {len(tables)} tables")
                return [
                    Resource(
                        uri=f"mssql://{table[0]}/data",
                        name=f"Table: {table[0]}",
                        mimeType="text/plain",
                        description=f"Data in table: {table[0]}",
                    )
                    for table in tables
                ]
        finally:
            conn.rollback()
            conn.close()
    except Exception as e:
        logger.error(f"Failed to list resources: {str(e)}")
        return []


@app.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    """Read table contents (TOP 100)."""
    uri_str = str(uri)
    logger.info(f"Reading resource: {uri_str}")
    if not uri_str.startswith("mssql://"):
        raise ValueError(f"Invalid URI scheme: {uri_str}")

    table = uri_str[8:].split('/')[0]
    # กัน injection ผ่านชื่อ table: อนุญาตเฉพาะ [schema.]name ที่เป็นตัวอักษร/ตัวเลข/_
    if not re.fullmatch(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)?", table):
        raise ValueError(f"Invalid table name: {table}")

    try:
        conn, _ = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT TOP 100 * FROM {table}")
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                result = [",".join(map(str, row)) for row in rows]
                return "\n".join([",".join(columns)] + result)
        finally:
            conn.rollback()
            conn.close()
    except Error as e:
        logger.error(f"Database error reading resource {uri}: {str(e)}")
        raise RuntimeError(f"Database error: {str(e)}")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available MSSQL tools."""
    logger.info("Listing tools...")
    return [
        Tool(
            name="execute_sql",
            description="Execute a read-only SQL query (SELECT / WITH) on the MSSQL server",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The SQL query to execute (read-only)",
                    }
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="explain_query",
            description=(
                "Capture a SQL Server execution plan for a query or stored procedure. "
                "mode='estimated' uses SET SHOWPLAN_XML (compiles only — nothing is executed) and "
                "accepts SELECT/WITH/EXEC/INSERT/UPDATE/DELETE/MERGE. "
                "mode='actual' uses SET STATISTICS XML + IO + TIME (really runs the statement, then "
                "rolls back) and accepts SELECT/WITH only unless MSSQL_EXPLAIN_ALLOW_ACTUAL_EXEC=true. "
                "Returns a readable summary (costs, top operators, scans, missing indexes, warnings, "
                "logical reads) plus the path to the full .sqlplan file. "
                "Requires the SHOWPLAN permission on the database."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The statement to explain, e.g. a SELECT or EXEC dbo.MyProc @p = 1",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["estimated", "actual"],
                        "default": "estimated",
                        "description": "estimated = compile only (safe); actual = execute and collect runtime stats",
                    },
                    "include_xml": {
                        "type": "boolean",
                        "default": False,
                        "description": "Also inline the raw plan XML (large — the file path is usually enough)",
                    },
                    "max_rows": {
                        "type": "integer",
                        "default": 0,
                        "description": "mode='actual': how many result rows to echo back (0 = none)",
                    },
                },
                "required": ["query"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute SQL commands (read-only enforced)."""
    logger.info(f"Calling tool: {name}")
    if name == "explain_query":
        return await _explain_query(arguments)
    if name != "execute_sql":
        raise ValueError(f"Unknown tool: {name}")

    query = arguments.get("query")
    if not query:
        raise ValueError("Query is required")

    readonly = _is_readonly()
    if readonly:
        try:
            assert_read_only(query)
        except (PermissionError, ValueError) as e:
            logger.warning(f"Blocked non-read-only query: {e}")
            return [TextContent(type="text", text=f"Blocked (read-only server): {e}")]

    try:
        conn, s = get_connection()
        try:
            with conn.cursor() as cursor:
                if _strip_sql(query).rstrip(";").strip().upper() == "SHOW TABLES":
                    cursor.execute(
                        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE';"
                    )
                    tables = cursor.fetchall()
                    result = [f"Tables_in_{s['database']}"] + [t[0] for t in tables]
                    return [TextContent(type="text", text="\n".join(result))]

                cursor.execute(query)
                if cursor.description is not None:
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()
                    result = [",".join(map(str, row)) for row in rows]
                    return [TextContent(type="text", text="\n".join([",".join(columns)] + result))]

                # ไม่มี result set (เช่นถ้าปิด read-only แล้วสั่ง write) — เราไม่ commit เด็ดขาด
                return [TextContent(
                    type="text",
                    text=(f"Statement executed (no result set). Rows affected: {cursor.rowcount}. "
                          f"NOTE: rolled back — this server never commits."),
                )]
        finally:
            # rollback เสมอ: ต่อให้มี write หลุด guard ก็ไม่ถูกบันทึก (ชั้นป้องกันที่สอง)
            conn.rollback()
            conn.close()
    except Exception as e:
        logger.error(f"Error executing SQL: {e}")
        return [TextContent(type="text", text=f"Error executing query: {str(e)}")]


async def main():
    """Main entry point to run the MCP server."""
    from mcp.server.stdio import stdio_server

    logger.info("Starting MSSQL MCP server...")
    s = get_db_settings()
    logger.info(
        f"Config: {s['server']}/{s['database']} "
        f"(auth={s['mode']}, read_only={_is_readonly()})"
    )

    async with stdio_server() as (read_stream, write_stream):
        try:
            await app.run(read_stream, write_stream, app.create_initialization_options())
        except Exception as e:
            logger.error(f"Server error: {str(e)}", exc_info=True)
            raise


if __name__ == "__main__":
    asyncio.run(main())
