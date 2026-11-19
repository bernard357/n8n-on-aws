"""The ``n8n-on-aws`` command line."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from .config import LoadResult, load
from .config.models import Deployment
from .config.rules import Finding
from .plan import Plan, build_plan

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2

_MARK = {"error": "✗", "warning": "!", "info": "·"}


def _print_findings(findings: list[Finding], out: TextIO) -> None:
    for f in findings:
        print(
            f"  {_MARK[f.level]} {f.level:<7} {f.code:<4} {f.path}: {f.message}",
            file=out,
        )
        if f.hint:
            print(f"                      → {f.hint}", file=out)


def _summary(path: str, result: LoadResult, out: TextIO) -> None:
    errors = sum(f.level == "error" for f in result.findings)
    warnings = sum(f.level == "warning" for f in result.findings)
    status = "valid" if result.ok else "invalid"
    print(f"{path}: {status} ({errors} errors, {warnings} warnings)", file=out)
    _print_findings(result.findings, out)


def _print_plan(plan: Plan, show_env: bool, out: TextIO) -> None:
    print(
        f"\nDeployment {plan.name}  (profile {plan.profile}, {plan.environment})",
        file=out,
    )
    purpose = f", usage {plan.purpose}" if plan.purpose else ""
    print(f"  n8n edition : {plan.edition}{purpose}", file=out)
    print(f"  URL         : {plan.url}", file=out)
    print(f"  runtime     : {plan.platform}", file=out)
    print(f"  stacks      : {', '.join(plan.stacks)}", file=out)
    services = ", ".join(f"{k} {v}" for k, v in plan.data_services.items())
    print(f"  data        : {services}", file=out)
    if plan.public_paths:
        print(f"  open paths  : {' '.join(plan.public_paths)}", file=out)
    dashboard = f"dashboard {plan.name}" if plan.dashboard else "no dashboard"
    notify = (
        "alarms email " + ", ".join(plan.alarm_emails)
        if plan.alarm_emails
        else "alarms notify nobody"
    )
    print(f"  operations  : {dashboard}; {notify}", file=out)
    print("  roles:", file=out)
    for role in plan.roles:
        replicas = (
            str(role.replicas_min)
            if role.replicas_min == role.replicas_max
            else f"{role.replicas_min}..{role.replicas_max}"
        )
        print(
            f"    - {role.name:<8} {role.command:<30} replicas {replicas:<6} "
            f"{role.cpu}cpu/{role.memory}MiB  scaling: {role.scaling}",
            file=out,
        )
        if show_env:
            for key, value in sorted(role.env.items()):
                print(f"        {key}={value}", file=out)
            for key, ref in sorted(role.secrets.items()):
                suffix = f":{ref.key}" if ref.key else ""
                print(f"        {key}=<secret {ref.secret}{suffix}>", file=out)
    if plan.findings:
        print("  plan notes:", file=out)
        _print_findings(plan.findings, out)


def _load(args: argparse.Namespace, out: TextIO) -> LoadResult | None:
    path = Path(args.file)
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return None
    try:
        return load(path, args.set)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def cmd_validate(args: argparse.Namespace, out: TextIO) -> int:
    result = _load(args, out)
    if result is None:
        return EXIT_USAGE
    _summary(args.file, result, out)
    return EXIT_OK if result.ok else EXIT_INVALID


def cmd_plan(args: argparse.Namespace, out: TextIO) -> int:
    result = _load(args, out)
    if result is None:
        return EXIT_USAGE
    if args.format == "json":
        payload: dict[str, object] = {
            "valid": result.ok,
            "findings": [f.model_dump() for f in result.findings],
        }
        if result.ok and result.deployment is not None:
            payload["plan"] = build_plan(result.deployment).model_dump()
        print(json.dumps(payload, indent=2), file=out)
        return EXIT_OK if result.ok else EXIT_INVALID
    _summary(args.file, result, out)
    if not result.ok or result.deployment is None:
        return EXIT_INVALID
    _print_plan(build_plan(result.deployment), args.show_env, out)
    return EXIT_OK


def cmd_deploy(args: argparse.Namespace, out: TextIO) -> int:
    if not args.dry_run:
        return _run_cdk("deploy", args, out)
    args.format = "text"
    code = cmd_plan(args, out)
    if code != EXIT_OK:
        return code
    result = load(args.file, args.set)
    assert result.deployment is not None
    from .cdk import synthesize  # CDK starts a Node.js process: import lazily

    deployment = result.deployment
    outdir = Path(args.output or f"cdk.out/{deployment.name}")
    stacks, findings = synthesize(deployment, outdir)
    if findings:
        print("  synthesis notes:", file=out)
        _print_findings(findings, out)
    if any(f.level == "error" for f in findings):
        return EXIT_INVALID
    print("\nStacks:", file=out)
    for stack in stacks:
        total = sum(stack.resources.values())
        print(f"  - {stack.name} ({stack.region}): {total} resources", file=out)
        for kind, count in stack.resources.items():
            print(f"      {count:>3} {kind}", file=out)
        if stack.dashboard:
            print(f"      dashboard: {', '.join(stack.dashboard)}", file=out)
    print(
        f"\nDry run: templates written to {outdir}; nothing was deployed and "
        "no AWS API was called.",
        file=out,
    )
    return EXIT_OK


def cdk_command(verb: str, args: argparse.Namespace, name: str) -> list[str]:
    """The CDK CLI command line for ``verb`` (deploy, diff, destroy)."""
    app = [sys.executable, "-m", "n8n_on_aws.cdk", "-f", str(Path(args.file).resolve())]
    for assignment in args.set:
        app += ["--set", assignment]
    command = [
        "npx",
        "--yes",
        "aws-cdk@2",
        verb,
        "--all",
        "--app",
        shlex.join(app),
        "--output",
        f"cdk.out/{name}",
    ]
    if verb == "deploy":
        approval = "never" if args.yes else "broadening"
        command += ["--require-approval", approval]
    if verb == "destroy" and args.yes:
        command.append("--force")
    return command


def _run_cdk(verb: str, args: argparse.Namespace, out: TextIO) -> int:
    result = _load(args, out)
    if result is None:
        return EXIT_USAGE
    if not result.ok or result.deployment is None:
        _summary(args.file, result, out)
        return EXIT_INVALID
    from .cdk import synthesis_findings

    blocking = [f for f in synthesis_findings(result.deployment) if f.level == "error"]
    if blocking:
        _print_findings(blocking, out)
        return EXIT_INVALID
    if shutil.which("npx") is None:
        print(
            "error: npx not found; install Node.js to run the CDK CLI", file=sys.stderr
        )
        return EXIT_USAGE
    command = cdk_command(verb, args, result.deployment.name)
    if verb == "deploy":
        from .cdk import bootstrap_regions

        regions = bootstrap_regions(result.deployment)
        print(
            "note: the CDK must be bootstrapped in "
            + ", ".join(regions)
            + " (npx aws-cdk@2 bootstrap aws://ACCOUNT/REGION)",
            file=out,
        )
    print("$ " + shlex.join(command), file=out)
    if verb == "destroy":
        print(
            "note: the encryption key secret is always retained (delete it yourself "
            "only if no backup needs it); the database snapshot, the bucket and EFS "
            "are retained outside dev environments; CDK's own default-security-group "
            "Lambda leaves a log group under /aws/lambda/<name>-network-",
            file=out,
        )
        if result.deployment.ingress.certificate.kind == "create":
            print(
                "note: the certificate's DNS validation record (_<hash>.<domain> "
                "CNAME) stays in the hosted zone; delete it if the domain is gone",
                file=out,
            )
    out.flush()
    return subprocess.call(command)


def cmd_diff(args: argparse.Namespace, out: TextIO) -> int:
    return _run_cdk("diff", args, out)


def cmd_destroy(args: argparse.Namespace, out: TextIO) -> int:
    return _run_cdk("destroy", args, out)


def cmd_schema(args: argparse.Namespace, out: TextIO) -> int:
    schema = Deployment.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "n8n-on-aws configuration (v1)"
    text = json.dumps(schema, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}", file=out)
    else:
        out.write(text)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="n8n-on-aws",
        description="Deploy n8n on AWS from one YAML file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def with_file(p: argparse.ArgumentParser) -> None:
        p.add_argument("-f", "--file", required=True, help="configuration file")
        p.add_argument(
            "--set",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help="override a value, e.g. --set topology.mode=queue (repeatable)",
        )

    p = sub.add_parser("validate", help="check the file against schema and rules")
    with_file(p)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("plan", help="show the resolved deployment")
    with_file(p)
    p.add_argument("--show-env", action="store_true", help="print n8n variables")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("deploy", help="deploy to AWS with the CDK CLI")
    with_file(p)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="validate, plan and synthesize locally; no AWS call",
    )
    p.add_argument("--show-env", action="store_true", help="print n8n variables")
    p.add_argument("-o", "--output", help="where to write templates (dry run)")
    p.add_argument(
        "--yes", action="store_true", help="do not ask to approve IAM changes"
    )
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("diff", help="compare with the deployed stacks")
    with_file(p)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("destroy", help="delete the stacks (data is retained)")
    with_file(p)
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_destroy)

    p = sub.add_parser("schema", help="print the JSON Schema of the file")
    p.add_argument("-o", "--output", help="write to this file")
    p.set_defaults(func=cmd_schema)
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    """Entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    code: int = args.func(args, out or sys.stdout)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
