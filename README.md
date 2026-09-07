![Tests](https://github.com/JexinSam/mssql_mcp_server/actions/workflows/test.yml/badge.svg)

# MSSQL MCP Server

MSSQL MCP Server is a **Model Context Protocol (MCP) server** that enables secure and structured interaction with **Microsoft SQL Server (MSSQL)** databases. It allows AI assistants to:
- List available tables
- Read table contents
- Execute SQL queries with controlled access
- Capture execution plans (SHOWPLAN / STATISTICS) for query tuning

This ensures safer database exploration, strict permission enforcement, and logging of database interactions.

## Features

- **Secure MSSQL Database Access** through environment variables
- **Read-only by default** — non-`SELECT`/`WITH` statements are rejected and every connection is rolled back
- **Controlled Query Execution** with error handling
- **Table Listing & Data Retrieval**
- **Execution plan capture** with a readable summary (`explain_query`)
- **Comprehensive Logging** for monitoring queries and operations

## Tools

### `execute_sql`

Runs a read-only query. Only `SELECT` / `WITH` (and `SHOW TABLES`) are accepted; stacked statements
and `SELECT ... INTO` are rejected, and the connection is always rolled back — the server never commits.

### `explain_query`

Captures a SQL Server execution plan and returns a readable summary plus the path to the full
`.sqlplan` file (openable in SSMS or any plan viewer).

| Parameter | Default | Description |
|-----------|---------|-------------|
| `query` | — | The statement to explain |
| `mode` | `estimated` | `estimated` = `SET SHOWPLAN_XML` (compile only, nothing runs); `actual` = `SET STATISTICS XML` + `IO` + `TIME` (really runs, then rolls back) |
| `include_xml` | `false` | Also inline the raw plan XML (usually large — the saved file is enough) |
| `max_rows` | `0` | `mode='actual'`: how many result rows to echo back |

The summary contains, per statement: subtree cost, compile time, memory grant, missing-index
suggestions, optimizer warnings (implicit conversions, tempdb spills, missing statistics), and the top
operators by *own* cost — flagged with `‼ scan` for full scans and, in `actual` mode, `‼ estimate skew`
where actual rows diverge from the estimate by more than 10×. `actual` mode also reports
`STATISTICS TIME` and per-table `STATISTICS IO` logical reads.

**What each mode accepts**

| | `estimated` | `actual` |
|---|---|---|
| `SELECT` / `WITH` | ✅ | ✅ |
| `EXEC` / `INSERT` / `UPDATE` / `DELETE` / `MERGE` | ✅ — SHOWPLAN compiles without executing | ❌ unless `MSSQL_EXPLAIN_ALLOW_ACTUAL_EXEC=true` |
| DDL, `DBCC`, `BACKUP`, `USE`, stacked statements | ❌ | ❌ |

Before running anything in `estimated` mode the server sends a canary (`SELECT 1`) and verifies the
server answered with plan XML rather than a value. If `SET SHOWPLAN_XML ON` did not engage — e.g. the
login lacks the `SHOWPLAN` permission — it aborts instead of executing the statement for real.

`explain_query` requires the **`SHOWPLAN`** permission on the database (`GRANT SHOWPLAN TO [<user>]`).

## Installation

```bash
pip install mssql-mcp-server
```

## Configuration

Set the following environment variables to configure database access:

```bash
MSSQL_DRIVER=mssql_driver
MSSQL_HOST=localhost
MSSQL_USER=your_username
MSSQL_PASSWORD=your_password
MSSQL_DATABASE=your_database
#optional
TrustServerCertificate=yes
Trusted_Connection=no
```

Azure AD (`az login`) authentication instead of SQL auth:

```bash
MSSQL_AUTHENTICATION=azure   # drops MSSQL_USER / MSSQL_PASSWORD, uses the current az session
```

Optional flags:

| Variable | Default | Effect |
|----------|---------|--------|
| `MSSQL_READONLY` | `true` | Set to `false` to lift the `execute_sql` read-only guard (the connection is still never committed) |
| `MSSQL_EXPLAIN_ALLOW_EXEC` | `true` | Allow `EXEC`/DML in `explain_query` `mode='estimated'` (safe — SHOWPLAN does not execute) |
| `MSSQL_EXPLAIN_ALLOW_ACTUAL_EXEC` | `false` | Allow `EXEC`/DML in `mode='actual'`, which really runs the statement before rolling back |
| `MSSQL_PLAN_DIR` | `<tmp>/mssql_mcp_plans` | Where `.sqlplan` files are written |
| `MSSQL_PLAN_INLINE_CHARS` | `200000` | Cap on inlined plan XML when `include_xml: true` |
| `MSSQL_ODBC_POOLING` | `true` | Set to `false` to disable ODBC connection pooling (slower, but removes any chance of a `SET` option surviving into the next call) |

## Usage

### With Claude Desktop

To integrate with **Claude Desktop**, add this configuration to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mssql": {
      "command": "uv",
      "args": [
        "--directory",
        "path/to/mssql_mcp_server",
        "run",
        "mssql_mcp_server"
      ],
      "env": {
        "MSSQL_DRIVER": "mssql_driver",
        "MSSQL_HOST": "localhost",
        "MSSQL_USER": "your_username",
        "MSSQL_PASSWORD": "your_password",
        "MSSQL_DATABASE": "your_database"
      }
    }
  }
}
```

### Running as a Standalone Server

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server
python -m mssql_mcp_server
```

## Development

```bash
# Clone the repository
git clone https://github.com/yourusername/mssql_mcp_server.git
cd mssql_mcp_server

# Set up a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install development dependencies
pip install -r requirements-dev.txt

# Run tests
pytest
```

## Security Considerations

- **Use a dedicated MSSQL user** with minimal privileges.
- **Never use root credentials** or full administrative accounts.
- **Restrict database access** to only necessary operations.
- **Enable logging and auditing** for security monitoring.
- **Regularly review permissions** to ensure least privilege access.

## Security Best Practices

For a secure setup:

1. **Create a dedicated MSSQL user** with restricted permissions.
2. **Avoid hardcoding credentials**—use environment variables instead.
3. **Restrict access** to necessary tables and operations only.
4. **Enable SQL Server logging and monitoring** for auditing.
5. **Review database access regularly** to prevent unauthorized access.

For detailed instructions, refer to the **[MSSQL Security Configuration Guide](https://github.com/JexinSam/mssql_mcp_server/blob/main/SECURITY.md)**.

⚠️ **IMPORTANT:** Always follow the **Principle of Least Privilege** when configuring database access.

## License

This project is licensed under the **MIT License**. See the `LICENSE` file for details.

## Contributing

We welcome contributions! To contribute:

1. Fork the repository.
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Commit your changes: `git commit -m 'Add amazing feature'`
4. Push to the branch: `git push origin feature/amazing-feature`
5. Open a **Pull Request**.

---

### Need Help?
For any questions or issues, feel free to open a GitHub **[Issue](https://github.com/JexinSam/mssql_mcp_server/issues)** or reach out to the maintainers.

