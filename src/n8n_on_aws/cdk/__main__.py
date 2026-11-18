"""CDK app entry point: ``cdk --app "python -m n8n_on_aws.cdk -f FILE"``."""

from __future__ import annotations

import argparse
import sys

from ..config import load
from .app import build_app


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m n8n_on_aws.cdk")
    parser.add_argument("-f", "--file", required=True)
    parser.add_argument("--set", action="append", default=[])
    args = parser.parse_args()
    result = load(args.file, args.set)
    if not result.ok or result.deployment is None:
        for finding in result.findings:
            if finding.level == "error":
                print(
                    f"{finding.code} {finding.path}: {finding.message}", file=sys.stderr
                )
        return 1
    app, findings = build_app(result.deployment)
    if app is None:
        for finding in findings:
            if finding.level == "error":
                print(
                    f"{finding.code} {finding.path}: {finding.message}", file=sys.stderr
                )
        return 1
    app.synth()
    return 0


if __name__ == "__main__":
    sys.exit(main())
