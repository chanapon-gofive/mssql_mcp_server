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
    assert names == {"execute_sql", "explain_query", "export_module", "compare_module"}
    for t in tools:
        for req in t.inputSchema["required"]:
            assert req in t.inputSchema["properties"]

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

# ---- export_module / compare_module ------------------------------------------

import hashlib
from mssql_mcp_server import server
from mssql_mcp_server.server import _MODULE_SQL, _text_stats

# CRLF + LF ปนกัน, space/tab ท้ายบรรทัด, บรรทัดที่มีแต่ tab, ภาษาไทย, emoji (surrogate pair)
DEF = (
    "-- =============================================\r\n"
    "-- Author: ทดสอบ\t\r\n"
    "CREATE PROCEDURE dbo.Foo  \r\n"
    "AS\r\n"
    "BEGIN\r\n"
    "\t\r\n"
    "    SELECT N'ภาษาไทย 😀' AS SENTINEL_BODY_TEXT \t\r\n"
    "END\n"
)


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, *params):
        self.conn.executed.append((sql, params))
        if self.conn.fail:
            raise RuntimeError("boom")
        return self

    def fetchone(self):
        return self.conn.row


class _FakeConn:
    def __init__(self, row, fail):
        self.row, self.fail = row, fail
        self.executed, self.rollbacks, self.commits, self.closed = [], 0, 0, False

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


@pytest.fixture
def export_root(tmp_path, monkeypatch):
    root = tmp_path / "plans" / "sql"
    root.mkdir(parents=True)
    monkeypatch.setenv("MSSQL_EXPORT_ROOT", str(root))
    return root


@pytest.fixture
def fake_db(monkeypatch):
    """แทน get_connection — คำนวณ length/hash แบบที่ HASHBYTES ทำ เพื่อจำลองค่าที่ server ส่งมา"""
    conns = []

    def install(definition=DEF, server_hash=None, fail=False, missing=False):
        data = definition.encode("utf-16-le")
        row = None if missing else (
            "dbo.Foo", "SQL_STORED_PROCEDURE", definition, len(data) // 2,
            server_hash or hashlib.sha256(data).hexdigest().upper(),
        )

        def get_connection():
            c = _FakeConn(row, fail)
            conns.append(c)
            return c, {"server": "fake-host", "database": "Empeo_Test"}

        monkeypatch.setattr(server, "get_connection", get_connection)
        return conns

    return install


async def _call(name, **args):
    return (await call_tool(name, args))[0].text


def test_text_stats_matches_hashbytes():
    # ค่าจาก SQL Server จริง: HASHBYTES('SHA2_256', N'ก' + NCHAR(9) + N'x ' + NCHAR(13) + NCHAR(10) + NCHAR(55357) + NCHAR(56832))
    st = _text_stats("ก\tx \r\n😀")
    assert st["sha256"] == "ED4C2A6354366D08B9C53DEDB4EBF476BDC2584A7D807F145B569F473DAAF0F2"
    assert (st["chars"], st["lines"], st["crlf"], st["lf"]) == (8, 2, 1, 0)


def test_module_sql_passes_read_only_guard():
    assert_read_only(_MODULE_SQL)


@pytest.mark.asyncio
async def test_export_module_writes_exact_text(export_root, fake_db):
    conns = fake_db()
    out = await _call("export_module", object_name="dbo.Foo", path="empeo-api/feat/proc_dbo_Foo.sql")

    f = export_root / "empeo-api" / "feat" / "proc_dbo_Foo.sql"
    raw = f.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8") == DEF
    with open(f, encoding="utf-8", newline="") as fh:
        assert fh.read() == DEF
    assert [p.name for p in f.parent.iterdir()] == ["proc_dbo_Foo.sql"]  # ไม่มี temp file ค้าง

    st = _text_stats(DEF)
    assert st["sha256"] in out
    assert f"chars:  {st['chars']:,}" in out
    assert "lines:  8 (CRLF 7 · LF 1)" in out
    assert str(f.resolve()) in out
    # ห้ามส่งเนื้อหาของ proc กลับมา
    assert "SENTINEL_BODY_TEXT" not in out and "ภาษาไทย" not in out and "CREATE PROCEDURE" not in out
    assert conns[0].executed == [(_MODULE_SQL, ("dbo.Foo",))]


@pytest.mark.asyncio
async def test_export_module_large_definition(export_root, fake_db):
    body = "CREATE PROCEDURE dbo.Foo AS\r\nBEGIN\r\n" + "    SET @x = @x + 1 -- ค่า \t \r\n" * 25000 + "END"
    assert len(body) > 716_010
    fake_db(body)
    out = await _call("export_module", object_name="dbo.Foo", path="big.sql")
    with open(export_root / "big.sql", encoding="utf-8", newline="") as fh:
        assert fh.read() == body
    assert _text_stats(body)["sha256"] in out


@pytest.mark.asyncio
async def test_export_module_refuses_existing_file(export_root, fake_db):
    conns = fake_db()
    f = export_root / "proc_dbo_Foo.sql"
    f.write_text("-- edited by the user\n")
    out = await _call("export_module", object_name="dbo.Foo", path="proc_dbo_Foo.sql")
    assert out.startswith("Blocked:") and "overwrite" in out
    assert f.read_text() == "-- edited by the user\n"
    assert conns == []

    await _call("export_module", object_name="dbo.Foo", path="proc_dbo_Foo.sql", overwrite=True)
    assert f.read_bytes().decode("utf-8") == DEF


@pytest.mark.asyncio
async def test_export_module_object_not_found(export_root, fake_db):
    fake_db(missing=True)
    out = await _call("export_module", object_name="dbo.Nope", path="x.sql")
    assert out.startswith("Error:") and "dbo.Nope" in out
    assert not (export_root / "x.sql").exists()


@pytest.mark.asyncio
async def test_export_module_refuses_text_that_differs_from_server(export_root, fake_db):
    fake_db(server_hash="00" * 32)
    out = await _call("export_module", object_name="dbo.Foo", path="x.sql")
    assert "does not match the server" in out and "nothing written" in out
    assert not (export_root / "x.sql").exists()


def _outside_paths(root):
    tmp = root.parent.parent
    (tmp / "outside").mkdir()
    (root / "link_dir").symlink_to(tmp / "outside", target_is_directory=True)
    (tmp / "outside.sql").write_text("x")
    (root / "link_file.sql").symlink_to(tmp / "outside.sql")
    return [
        str(tmp / "evil.sql"),                    # path เต็มนอก root
        "../evil.sql",
        "a/../../evil.sql",
        str(tmp / "plans" / "sqlx" / "evil.sql"),  # ชื่อขึ้นต้นเหมือน root แต่เป็นโฟลเดอร์อื่น
        "link_dir/evil.sql",                      # symlink โฟลเดอร์ชี้ออกนอก root
        "link_file.sql",                          # symlink ไฟล์ชี้ออกนอก root
        "~/evil.sql",
        ".",
        "proc_dbo_Foo.txt",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["export_module", "compare_module"])
async def test_module_tools_reject_paths_outside_root(tool, export_root, fake_db):
    conns = fake_db()
    for path in _outside_paths(export_root):
        out = await _call(tool, object_name="dbo.Foo", path=path)
        assert out.startswith("Blocked:"), (path, out)
    assert conns == []  # ปฏิเสธก่อนต่อ DB
    assert (export_root.parent.parent / "outside.sql").read_text() == "x"
    assert not (export_root.parent.parent / "evil.sql").exists()
    assert not (export_root.parent.parent / "outside" / "evil.sql").exists()


@pytest.mark.asyncio
async def test_module_tools_require_arguments(export_root, fake_db):
    fake_db()
    with pytest.raises(ValueError, match="object_name"):
        await call_tool("export_module", {"path": "x.sql"})
    with pytest.raises(ValueError, match="path"):
        await call_tool("compare_module", {"object_name": "dbo.Foo"})


@pytest.mark.asyncio
async def test_module_tools_are_read_only_and_always_roll_back(export_root, fake_db):
    conns = fake_db()
    await _call("export_module", object_name="dbo.Foo", path="x.sql")
    await _call("compare_module", object_name="dbo.Foo", path="x.sql")
    assert len(conns) == 2
    for c in conns:
        for sql, _ in c.executed:
            assert_read_only(sql)
        assert (c.commits, c.rollbacks, c.closed) == (0, 1, True)


@pytest.mark.asyncio
async def test_module_tools_roll_back_on_error(export_root, fake_db):
    conns = fake_db(fail=True)
    out = await _call("export_module", object_name="dbo.Foo", path="x.sql")
    assert "boom" in out
    assert not (export_root / "x.sql").exists()
    (export_root / "y.sql").write_text(DEF)
    assert "boom" in await _call("compare_module", object_name="dbo.Foo", path="y.sql")
    assert [(c.commits, c.rollbacks, c.closed) for c in conns] == [(0, 1, True)] * 2


@pytest.mark.asyncio
async def test_compare_module_match(export_root, fake_db):
    fake_db()
    await _call("export_module", object_name="dbo.Foo", path="x.sql")
    out = await _call("compare_module", object_name="dbo.Foo", path=str(export_root / "x.sql"))
    assert "verdict: MATCH" in out
    assert "SENTINEL_BODY_TEXT" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding,label", [("utf-8-sig", "utf-8 (BOM)"), ("utf-16", "utf-16 (BOM)")])
async def test_compare_module_ignores_bom(encoding, label, export_root, fake_db):
    fake_db()
    (export_root / "x.sql").write_bytes(DEF.encode(encoding))
    out = await _call("compare_module", object_name="dbo.Foo", path="x.sql")
    assert "verdict: MATCH" in out and label in out


@pytest.mark.asyncio
async def test_compare_module_trailing_whitespace(export_root, fake_db):
    fake_db()
    (export_root / "x.sql").write_bytes(DEF.replace("dbo.Foo  \r\n", "dbo.Foo\r\n").encode())
    out = await _call("compare_module", object_name="dbo.Foo", path="x.sql")
    assert "verdict: DIFFERENT — whitespace only" in out
    assert "first difference: line 3 — trailing whitespace only" in out
    # ความยาว + hash 8 ตัวแรกของบรรทัด เทียบกับ STRING_SPLIT(definition, CHAR(10), 1) ได้
    assert f"len 27, h {hashlib.sha256('CREATE PROCEDURE dbo.Foo  \r'.encode('utf-16-le')).hexdigest().upper()[:8]}" in out


@pytest.mark.asyncio
async def test_compare_module_line_endings(export_root, fake_db):
    fake_db()
    (export_root / "x.sql").write_bytes(DEF.replace("\r\n", "\n").encode())
    out = await _call("compare_module", object_name="dbo.Foo", path="x.sql")
    assert "whitespace only" in out
    assert "first difference: line 1 — line ending only" in out


@pytest.mark.asyncio
async def test_compare_module_content(export_root, fake_db):
    fake_db()
    (export_root / "x.sql").write_bytes(DEF.replace("SELECT N'", "SELECT TOP 1 N'").encode())
    out = await _call("compare_module", object_name="dbo.Foo", path="x.sql")
    assert "verdict: DIFFERENT — content differs" in out
    assert "first difference: line 7 — content, from column 12" in out
    assert "hunks: 1 — replace DB 7 ↔ file 7" in out


@pytest.mark.asyncio
async def test_compare_module_file_problems(export_root, fake_db):
    conns = fake_db()
    assert "file not found" in await _call("compare_module", object_name="dbo.Foo", path="nope.sql")
    (export_root / "bad.sql").write_bytes(b"\xff\x00\xfe")
    assert "not UTF-8/UTF-16" in await _call("compare_module", object_name="dbo.Foo", path="bad.sql")
    assert conns == []


@pytest.mark.asyncio
async def test_compare_module_inserted_and_deleted_lines(export_root, fake_db):
    fake_db()
    lines = DEF.split("\n")
    lines[4:4] = ["-- new 1\r", "-- new 2\r"]
    del lines[8]
    (export_root / "x.sql").write_bytes("\n".join(lines).encode())
    out = await _call("compare_module", object_name="dbo.Foo", path="x.sql")
    assert "first difference: line 5 — content, from column 1" in out
    assert "insert DB (before 5) ↔ file 5-6" in out
    assert "delete DB 7 ↔ file (before 9)" in out
