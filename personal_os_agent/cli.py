"""Professional CLI entry point for the Personal OS Agent."""

import argparse
import asyncio
from typing import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PersonalOS Deadline Tracker")
    parser.add_argument(
        "--skip-poll",
        action="store_true",
        help="Disable ObserverAgent polling",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Run the dashboard and reminder worker without Gmail or LLM agents",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    from personal_os_agent.utils.logger import setup_logging

    setup_logging()

    if not args.no_mcp:
        print("[Main] Starting MCP tool server thread...")
        from personal_os_agent.core.bootstrap import start_mcp_server_thread

        start_mcp_server_thread()

    from personal_os_agent.core.bootstrap import run

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    finally:
        print("[Main] Stopped.")


if __name__ == "__main__":
    main()
