import asyncio
import json
import logging
import os
import re
import struct
import subprocess
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
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute SQL commands (read-only enforced)."""
    logger.info(f"Calling tool: {name}")
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
