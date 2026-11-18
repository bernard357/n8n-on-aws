# 5. CLI and assistant mode

## 5.1 Verbs

```text
n8n-on-aws init      [-o n8n.yaml] [--from minimal|nano|...] [--plain]   # assistant
n8n-on-aws validate  -f FILE                          # schema + n8n rules, no AWS calls
n8n-on-aws plan      -f FILE [--cost] [--show-env]    # resolved config + provenance, stacks, roles
n8n-on-aws deploy    -f FILE [--dry-run] [--set k=v]… [--yes] [--require-approval never|broadening]
n8n-on-aws diff      -f FILE                          # cdk diff against deployed stacks
n8n-on-aws status    -f FILE                          # stack states, URL, task/pod health
n8n-on-aws destroy   -f FILE [--keep-data]            # data stack retained unless asked
n8n-on-aws schema    [-o schema.json]                 # JSON Schema from the pydantic models
n8n-on-aws samples   [list | show NAME]               # the fixtures, shipped in the wheel
```

`deploy --dry-run` = `validate` + `plan` + `cdk synth` into `cdk.out/` + optional `cdk diff` when stacks
exist. It never calls CloudFormation write APIs. `deploy` runs `cdk bootstrap` checks, then `cdk deploy`
of the stacks in dependency order, and prints the n8n URL and next steps (first owner signup, Cognito
invitations).

The CDK app lives inside the package (`n8n_on_aws/app.py`). The CLI drives the CDK toolkit through a
subprocess (`npx aws-cdk@2`), or through the CDK Toolkit Library (`aws-cdk.toolkit-lib`) once that is
mature enough for Python. Users never see a `cdk.json`.

## 5.2 Assistant (`init`)

Goal: produce a **commented, minimal** YAML file. Only keys that differ from the chosen profile are
written, and every non-default value carries a comment explaining it. The question flow follows the
decision tree, not the schema:

1. **Name and region** (region defaults from the AWS profile).
2. **How big?** Shows the profile table with indicative cost and picks `nano` … `xlarge`.
3. **Edition and usage.** community, registered community, business or enterprise. Community editions
   ask about usage boundaries and acceptance of the Sustainable Use License; Business asks for the
   number of employees (questions in [06-licensing.md](06-licensing.md) §6.5). The edition then unlocks,
   hides or annotates later questions.
3b. **Budget.** Monthly ceiling and alert emails, with the profile's indicative cost shown
   ([07-finops.md](07-finops.md)). Non-production environments are offered office-hours schedules.
4. **Where should it run?** ECS (recommended) or EKS. For EKS: new cluster or existing (lists clusters
   with `eks:ListClusters`).
5. **Network?** New VPC, or pick an existing one (lists VPCs and subnets with `ec2:Describe*`).
6. **Database?** create (suggested kind from profile) or point at an existing one (lists RDS/Aurora,
   asks for the credentials secret, lists Secrets Manager names).
7. **Queue?** Asked only for queue topologies.
8. **Files?** S3 binary data (Business+), exchange bucket, new or existing (lists buckets).
9. **Domain?** Route 53 zones listed; certificate create or pick (lists ACM certificates).
10. **Ingress and login wall.** CloudFront (cheapest, Cognito via Lambda@Edge) or ALB (private
    networks, Cognito/OIDC built in), then none, IP allow-list, Cognito (new or pick a pool), or OIDC.
11. Summary + validation result + "Run `n8n-on-aws deploy -f n8n.yaml --dry-run` next."

Every lookup is read-only and optional: `--offline` turns lists into free-text prompts. The assistant
only builds a dict and passes it through the same pydantic models, so it cannot produce a file that
`validate` would reject.

**UI choice.** Start with **questionary** (or Rich prompts): simple and linear, works over SSH and in CI
logs, and is easy to test with stub answers. Add a **Textual** full-screen form later as `init --tui`
when the question tree stabilizes. Textual is worth it for live preview (YAML on the right, cost estimate
updating), but it costs more to build and test. Both front-ends share one `Wizard` model of
questions and conditions.

## 5.3 Package layout (proposed)

```text
n8n-on-aws/
├── src/n8n_on_aws/
│   ├── cli.py               # Typer/argparse entry point: n8n-on-aws
│   ├── config/              # pydantic models, profiles, rules, provenance, JSON schema
│   ├── plan.py              # config → roles, env vars, stacks (pure, unit-testable)
│   ├── stacks/              # CDK: network, data, edge, ecs_service, eks_release
│   ├── wizard/              # question model + questionary front-end (+ textual later)
│   └── cost.py              # Pricing API estimates (optional)
├── fixtures/configuration/  # samples, validated and synthesized in CI
├── schema/                  # exported JSON schema
├── docs/                    # research now, user guide later
└── tests/
```

CI gate: every file in `fixtures/configuration/` must pass `validate`, and `cdk synth` must succeed
with snapshot tests on the generated templates (no AWS calls; lookups are stubbed through context).
