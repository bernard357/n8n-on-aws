# Changes

## Unreleased

- Configuration model for `n8n-on-aws/v1`, as strict pydantic models with kind-discriminated
  `create` / `existing` / `external` resources, and `nano` to `xlarge` profiles.
- Rule engine:
  - topology and AWS rules (`T1`–`T15`)
  - n8n licence enforcement (`L1`–`L10`): community usage boundaries, edition-gated
    features, and gates that also apply to raw `n8n.env`
  - FinOps rules (`F1`–`F4`)
- CLI: `validate`, `plan` (text or JSON, `--show-env`), `deploy --dry-run`, `schema`,
  with `--set key=value` overrides.
- JSON Schema in `schema/n8n-on-aws.v1.json` for editor completion.
- Nine sample configurations, from `minimal` to `enterprise-ha`, on ECS and EKS.
- Research and design notes in `docs/research`.
