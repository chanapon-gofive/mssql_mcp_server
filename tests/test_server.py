import pytest
from mssql_mcp_server.server import (
    app, list_tools, list_resources, read_resource, call_tool,
    assert_read_only, assert_explain_safe, _stats_summary, _plan_summary,
)
from pydantic import AnyUrl

def test_server_initialization():
    """Test that the server initializes correctly."""
    assert app.name == "mssql_mcp_server"

@pytest.mark.asyncio
async def test_list_tools():
    """Test that list_tools returns expected tools."""
    tools = await list_tools()
    names = {t.name for t in tools}
    assert names == {"execute_sql", "explain_query"}
    for t in tools:
        assert "query" in t.inputSchema["properties"]

@pytest.mark.parametrize("query", [
    "SELECT 1",
    "WITH c AS (SELECT 1 AS a) SELECT * FROM c",
])
def test_read_only_allows_reads(query):
    assert_read_only(query)

@pytest.mark.parametrize("query", [
    "DELETE FROM t",
    "EXEC dbo.Foo",
    "SELECT 1; DROP TABLE t",
    "SELECT * INTO x FROM y",
])
def test_read_only_blocks_writes(query):
    with pytest.raises((PermissionError, ValueError)):
        assert_read_only(query)

@pytest.mark.parametrize("query,mode", [
    ("SELECT 1", "estimated"),
    ("SELECT 1", "actual"),
    ("EXEC dbo.Foo @a = 1", "estimated"),
    ("UPDATE t SET a = 1", "estimated"),
    ("/* c */ -- x\nEXEC dbo.Foo", "estimated"),
])
def test_explain_allows(query, mode):
    assert_explain_safe(query, mode)

@pytest.mark.parametrize("query,mode", [
    ("EXEC dbo.Foo", "actual"),           # actual รันจริง → ต้องเปิด env ก่อน
    ("UPDATE t SET a = 1", "actual"),
    ("DROP TABLE t", "estimated"),
    ("ALTER PROCEDURE dbo.Foo AS SELECT 1", "estimated"),
    ("SELECT 1; DROP TABLE t", "estimated"),
    ("SELECT * INTO x FROM y", "actual"),
    ("", "estimated"),
])
def test_explain_blocks(query, mode):
    with pytest.raises((PermissionError, ValueError)):
        assert_explain_safe(query, mode)

def test_explain_actual_exec_opt_in(monkeypatch):
    monkeypatch.setenv("MSSQL_EXPLAIN_ALLOW_ACTUAL_EXEC", "true")
    assert_explain_safe("EXEC dbo.Foo", "actual")

def test_explain_estimated_exec_opt_out(monkeypatch):
    monkeypatch.setenv("MSSQL_EXPLAIN_ALLOW_EXEC", "false")
    with pytest.raises(PermissionError):
        assert_explain_safe("EXEC dbo.Foo", "estimated")

def test_stats_summary_parses_io_and_time():
    msgs = [
        "SQL Server parse and compile time: \n   CPU time = 5 ms, elapsed time = 7 ms.",
        "Table '[sys].[sysschobjs]'. Scan count 1, logical reads 74, physical reads 0, "
        "lob logical reads 3, lob physical reads 0.",
        "Table '[sys].[sysschobjs]'. Scan count 1, logical reads 74, physical reads 0, "
        "lob logical reads 3, lob physical reads 0.",   # ซ้ำจาก sweep() ต้องถูกตัดทิ้ง
        "\n SQL Server Execution Times:\n   CPU time = 16 ms,  elapsed time = 2 ms.",
    ]
    out = "\n".join(_stats_summary(msgs))
    assert "execution CPU 16 ms" in out
    assert "parse/compile CPU 5 ms" in out
    assert "sys.sysschobjs" in out
    assert "74 reads" in out

def test_plan_summary_handles_bad_xml():
    assert "could not parse" in _plan_summary("<not-xml", False)[0]

@pytest.mark.asyncio
async def test_call_tool_invalid_name():
    """Test calling a tool with an invalid name."""
    with pytest.raises(ValueError, match="Unknown tool"):
        await call_tool("invalid_tool", {})

@pytest.mark.asyncio
async def test_call_tool_missing_query():
    """Test calling execute_sql without a query."""
    with pytest.raises(ValueError, match="Query is required"):
        await call_tool("execute_sql", {})

# Skip database-dependent tests if no database connection
@pytest.mark.asyncio
@pytest.mark.skipif(
    not all([
        pytest.importorskip("pyodbc"),
        pytest.importorskip("mssql_mcp_server")
    ]),
    reason="MSSQL connection not available"
)
async def test_list_resources():
    """Test listing resources (requires database connection)."""
    try:
        resources = await list_resources()
        assert isinstance(resources, list)
    except ValueError as e:
        if "Missing required database configuration" in str(e):
            pytest.skip("Database configuration not available")
        raise