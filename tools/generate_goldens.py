"""
tools/generate_goldens.py — CLI Script to Generate Visual Regression Goldens.

Usage:
    .\\.venv\\Scripts\\python.exe tools/generate_goldens.py [--output-dir tests/goldens]
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from tools.visual_gate_helper import generate_all_goldens, get_app


def main():
    parser = argparse.ArgumentParser(description="Generate Visual Regression Golden Images.")
    parser.add_argument(
        "--output-dir",
        default=os.path.join(BASE_DIR, "tests", "goldens"),
        help="Directory where golden images will be written.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-image console output.",
    )
    args = parser.parse_args()

    # Pre-initialize Qt application
    get_app()

    print(f"Generating Visual Regression Gate goldens into: {args.output_dir}")
    t0 = time.time()
    total = generate_all_goldens(args.output_dir, verbose=not args.quiet)
    elapsed = time.time() - t0
    print(f"Successfully generated {total} golden images in {elapsed:.2f}s.")


if __name__ == "__main__":
    main()
