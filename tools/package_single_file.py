#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# SlopBro
# by throwaway96
# https://github.com/throwaway96/slopbro
# Copyright 2026. Licensed under AGPL v3 or later. No warranties.

"""Build a single-file SlopBro package with embedded wwwroot assets.

Usage:
  python tools/package_single_file.py --out dist/slopbro_packed.py
  python tools/package_single_file.py --in-place
"""

import argparse
import base64
import json
import sys
from pathlib import Path

type AssetMap = dict[str, dict[str, str]]

BEGIN_MARKER = "# --- BEGIN EMBEDDED WWWROOT ---"
END_MARKER = "# --- END EMBEDDED WWWROOT ---"

DEFAULT_FILES = [
    "index.html",
    "autoroot.sh",
    "package.json",
    "main.js",
]


def _build_asset_map(wwwroot_dir: Path, file_list: list[str]) -> AssetMap:
    payload: AssetMap = {}
    for rel_path in file_list:
        abs_path = wwwroot_dir / rel_path
        if not abs_path.is_file():
            raise RuntimeError(f"missing asset: {abs_path}")
        encoded = base64.b64encode(abs_path.read_bytes()).decode("ascii")
        payload[rel_path] = {
            "encoding": "base64",
            "data": encoded,
        }
    return payload


def _replace_embedded_block(source_text: str, embedded_map: AssetMap) -> str:
    start = source_text.find(BEGIN_MARKER)
    end = source_text.find(END_MARKER)
    if start < 0 or end < 0 or end < start:
        raise RuntimeError("could not find embedded asset markers in source file")

    start_body = source_text.find("\n", start)
    if start_body < 0:
        raise RuntimeError("malformed begin marker")

    embedded_json = json.dumps(embedded_map, sort_keys=True, indent=4)
    replacement = f"\nEMBEDDED_WWWROOT = {embedded_json}\n"

    return source_text[: start_body + 1] + replacement + source_text[end:]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Embed wwwroot files into slopbro.py")
    parser.add_argument(
        "--source",
        default="slopbro.py",
        help="path to source slopbro.py",
    )
    parser.add_argument(
        "--wwwroot",
        default="wwwroot",
        help="path to wwwroot directory",
    )
    parser.add_argument(
        "--out",
        default="dist/slopbro_packed.py",
        help="output package file path",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="specific relative files to embed (defaults to required launcher files)",
    )
    args = parser.parse_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    source_path = Path(args.source).resolve()
    wwwroot_dir = Path(args.wwwroot).resolve()
    output_path = Path(args.out).resolve()
    files = args.files if args.files else list(DEFAULT_FILES)
    if not files:
        raise RuntimeError("no files selected to embed")

    source_text = _read_text(source_path)
    embedded = _build_asset_map(wwwroot_dir, files)
    output_text = _replace_embedded_block(source_text, embedded)
    _write_text(output_path, output_text)

    print(f"embedded {len(files)} files")
    print(f"source: {source_path}")
    print(f"wwwroot: {wwwroot_dir}")
    print(f"output: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
