"""Unified command line entrypoint for the pipeline."""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence

COMMAND_MODULES = {
    "export": "export_zotero",
    "download": "download_pdfs",
    "parse": "parse_pdfs",
    "summarize": "summarize_papers",
    "validate": "validate_mapping",
    "report": "report_pipeline",
}


def run_module(module_name: str, argv: Sequence[str]) -> None:
    module = importlib.import_module(module_name)
    old_argv = sys.argv[:]
    try:
        sys.argv = [module_name, *argv]
        module.main()
    finally:
        sys.argv = old_argv


def run_all(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description="Run the full Zotero paper pipeline.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-fail", action="store_true", help="do not fail the final validate step")
    parser.add_argument("--skip-export", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-parse", action="store_true")
    parser.add_argument("--skip-summarize", action="store_true")
    parser.add_argument("--skip-report", action="store_true")
    args = parser.parse_args(list(argv))

    common = ["--config", args.config]
    limit = ["--limit", str(args.limit)] if args.limit else []
    force = ["--force"] if args.force else []

    if not args.skip_export:
        run_module("export_zotero", [*common, *limit])
    if not args.skip_download:
        run_module("download_pdfs", [*common, *force, *limit])
    if not args.skip_parse:
        run_module("parse_pdfs", [*common, *force, *limit])
    if not args.skip_summarize:
        run_module("summarize_papers", [*common, *force, *limit])

    validate_args = [*common, "--format", "text"]
    if args.no_fail:
        validate_args.append("--no-fail")
    run_module("validate_mapping", validate_args)

    if not args.skip_report:
        run_module("report_pipeline", common)


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        commands = ", ".join([*COMMAND_MODULES, "run-all"])
        print(f"usage: python -m zotero_scan <command> [args]\n\ncommands: {commands}")
        return

    command, rest = args[0], args[1:]
    if command == "run-all":
        run_all(rest)
        return
    module_name = COMMAND_MODULES.get(command)
    if not module_name:
        raise SystemExit(f"unknown command: {command}")
    run_module(module_name, rest)
