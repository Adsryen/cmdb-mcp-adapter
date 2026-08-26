"""Allow python -m cmdb_mcp_adapter to invoke the CLI."""

import sys

from cmdb_mcp_adapter.cli import main

if __name__ == "__main__":
    sys.exit(main())
