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
from typing import TYPE_CHECKING, TextIO

from .config import LoadResult, load
from .config.models import Deployment
from .config.rules import Finding
from .plan import Plan, build_plan

if TYPE_CHECKING:
    from .finops.estimate import Estimate

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
    if plan.metrics_agent:
        print(
            "  n8n metrics : CloudWatch agent on main, namespace n8n-on-aws",
            file=out,
        )
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


def _cost(d: Deployment, plan: Plan) -> Estimate:
    from .finops.estimate import estimate

    return estimate(d, plan)


def _print_cost(cost: Estimate, out: TextIO) -> None:
    where = cost.region if cost.priced_in == cost.region else f"{cost.region}*"
    print(
        f"\nMonthly cost estimate (USD, {where}, prices of {cost.prices_date})",
        file=out,
    )
    print(f"  {'component':<14} {'fixed':>9} {'usage':>9}", file=out)
    for component, (fixed, usage) in cost.by_component().items():
        print(f"  {component:<14} {fixed:>9.2f} {usage:>9.2f}", file=out)
    print(
        f"  {'total':<14} {cost.fixed:>9.2f} {cost.usage:>9.2f}"
        f"   = {cost.total:.2f} a month",
        file=out,
    )
    print("  lines:", file=out)
    for item in cost.items:
        if not (item.fixed or item.usage):
            continue
        note = f"  ({item.note})" if item.note else ""
        print(
            f"    {item.component:<13} {item.fixed + item.usage:>8.2f}  "
            f"{item.item}{note}",
            file=out,
        )
    assumed = ", ".join(f"{k} {v:g}" for k, v in cost.assumptions.items())
    print(f"  assumptions: {assumed} (finops.usage)", file=out)
    for note in cost.notes:
        print(f"  note: {note}", file=out)
    print(
        "  fixed: at the minimum size with no traffic; usage: from the assumptions. "
        "Taxes, support plans and credits are not included.",
        file=out,
    )


def _ceiling(d: Deployment, cost: Estimate) -> Finding | None:
    """An error when the estimate exceeds finops.max_monthly_usd."""
    ceiling = d.finops.max_monthly_usd
    if ceiling is None or cost.total <= ceiling:
        return None
    return Finding(
        level="error",
        code="F2",
        path="finops.max_monthly_usd",
        message=f"the estimate is {cost.total:.2f} USD a month "
        f"({cost.fixed:.2f} fixed), above the ceiling of {ceiling:g}",
        hint="see `plan --cost`; lower the sizes or raise the ceiling",
    )


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
    want_cost = getattr(args, "cost", False)
    if args.format == "json":
        payload: dict[str, object] = {
            "valid": result.ok,
            "findings": [f.model_dump() for f in result.findings],
        }
        code = EXIT_OK if result.ok else EXIT_INVALID
        if result.ok and result.deployment is not None:
            plan = build_plan(result.deployment)
            payload["plan"] = plan.model_dump()
            cost = _cost(result.deployment, plan)
            if want_cost:
                payload["cost"] = cost.model_dump() | {
                    "fixed": cost.fixed,
                    "usage": cost.usage,
                    "total": cost.total,
                }
            over = _ceiling(result.deployment, cost)
            if over is not None:
                payload["valid"] = False
                payload["findings"].append(over.model_dump())  # type: ignore[attr-defined]
                code = EXIT_INVALID
        print(json.dumps(payload, indent=2), file=out)
        return code
    _summary(args.file, result, out)
    if not result.ok or result.deployment is None:
        return EXIT_INVALID
    plan = build_plan(result.deployment)
    _print_plan(plan, args.show_env, out)
    cost = _cost(result.deployment, plan)
    if want_cost:
        _print_cost(cost, out)
    else:
        print(
            f"\nEstimate: about {cost.total:.0f} USD a month "
            f"({cost.fixed:.0f} fixed); details with --cost",
            file=out,
        )
    over = _ceiling(result.deployment, cost)
    if over is not None:
        _print_findings([over], out)
        return EXIT_INVALID
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
    if verb == "deploy":
        over = _ceiling(
            result.deployment, _cost(result.deployment, build_plan(result.deployment))
        )
        if over is not None:
            blocking.append(over)
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
            "are retained outside dev environments; CDK's helper Lambdas leave log "
            "groups under /aws/lambda/<name>-, which `n8n-on-aws cleanup` deletes",
            file=out,
        )
        if result.deployment.auth.mode == "cloudfront-cognito":
            print(
                "note: the Lambda@Edge login wall is retained, because AWS deletes "
                "its replicas hours after the distribution is gone; run "
                f"`n8n-on-aws cleanup -f {args.file}` a few hours later",
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


def _aws(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["aws", *arguments], capture_output=True, text=True, check=False
    )


def cmd_cleanup(args: argparse.Namespace, out: TextIO) -> int:
    """Delete what outlives ``destroy``: Lambda log groups and the edge function.

    CDK's helper functions (cross-region references, the default security group)
    leave ``/aws/lambda/<name>-...`` log groups behind, and the Lambda@Edge login
    wall is retained until AWS has removed its replicas.
    """
    result = _load(args, out)
    if result is None or result.deployment is None:
        if result is not None:
            _summary(args.file, result, out)
        return EXIT_USAGE if result is None else EXIT_INVALID
    from .cdk import environment
    from .cdk.edge import EDGE_REGION, edge_auth_function_name, needs_edge_auth

    d = result.deployment
    if shutil.which("aws") is None:
        print("error: the AWS CLI is needed for cleanup", file=sys.stderr)
        return EXIT_USAGE
    edge = needs_edge_auth(d)
    name = edge_auth_function_name(d)
    if not args.yes:
        what = f"the Lambda@Edge function {name} (us-east-1) and its log groups, "
        print(
            f"Delete {what if edge else ''}and the /aws/lambda/{d.name}-* log groups "
            "left by CDK helpers? [y/N] ",
            end="",
            file=out,
        )
        out.flush()
        if input().strip().lower() not in ("y", "yes"):
            return EXIT_OK
    code = EXIT_OK
    region = environment(d).region or EDGE_REGION
    for where in dict.fromkeys([region, EDGE_REGION]):
        listed = _aws(
            "logs",
            "describe-log-groups",
            "--region",
            where,
            "--log-group-name-prefix",
            f"/aws/lambda/{d.name}-",
            "--query",
            "logGroups[].logGroupName",
            "--output",
            "text",
        )
        for group in listed.stdout.split():
            if (
                _aws(
                    "logs",
                    "delete-log-group",
                    "--region",
                    where,
                    "--log-group-name",
                    group,
                ).returncode
                == 0
            ):
                print(f"deleted log group {group} in {where}", file=out)
    if not edge:
        return code
    deleted = _aws(
        "lambda", "delete-function", "--region", EDGE_REGION, "--function-name", name
    )
    if deleted.returncode == 0:
        print(f"deleted function {name}", file=out)
    elif "ResourceNotFoundException" in deleted.stderr:
        print(f"function {name} is already gone", file=out)
    elif "replicated function" in deleted.stderr:
        print(
            f"function {name} still has replicas: AWS removes them a few hours "
            "after the distribution is deleted; run cleanup again later",
            file=out,
        )
        code = EXIT_INVALID
    else:
        print(f"error: {deleted.stderr.strip()}", file=sys.stderr)
        return EXIT_INVALID
    regions = _aws(
        "ec2",
        "describe-regions",
        "--region",
        EDGE_REGION,
        "--query",
        "Regions[].RegionName",
        "--output",
        "text",
    )
    if regions.returncode != 0:
        print(f"error: {regions.stderr.strip()}", file=sys.stderr)
        return EXIT_INVALID
    for where in regions.stdout.split():
        group = f"/aws/lambda/{EDGE_REGION}.{name}"
        if (
            _aws(
                "logs", "delete-log-group", "--region", where, "--log-group-name", group
            ).returncode
            == 0
        ):
            print(f"deleted log group {group} in {where}", file=out)
    return code


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
    p.add_argument(
        "--cost", action="store_true", help="detail the monthly cost estimate"
    )
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

    p = sub.add_parser(
        "cleanup", help="delete what outlives destroy (CDK log groups, edge function)"
    )
    with_file(p)
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_cleanup)

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
