# AGENTS.md — developing n8n-on-aws

`n8n-on-aws` turns one YAML file into an n8n deployment on AWS (CDK in Python, then
CloudFormation). This file is for an agent or agentic IDE **working in this
repository**. What the tool does and how to use it is in [README.md](README.md). The
design rationale is in [docs/research](docs/research). Link to those rather than
restating them.

## The Makefile is the toolbox

- `make setup`: create `.venv` with uv and install the package with dev tools
- `make lint` / `make format`: ruff, ruff format, mypy (strict), markdownlint
- `make test`: run the pytest suite
- `make validate`: validate every file in `fixtures/configuration/`
- `make schema`: regenerate `schema/n8n-on-aws.v1.json` after changing the models
- `make build`: build the wheel and sdist (version from the git tag via hatch-vcs)

## How to implement changes

Edit directly, run `make lint` and `make test`, and commit. The gate is
non-negotiable. A change to the grammar touches four places together: the models
(`config/models.py`), the rules (`config/rules.py`), the JSON Schema (`make schema`),
and the grammar doc (`docs/research/04-configuration-grammar.md`). The samples must
keep validating.

## House conventions

- **src layout.** The package lives in `src/n8n_on_aws/`.
- **Invoke venv binaries directly**: `.venv/bin/pytest`, never `source .venv/bin/activate`.
- **pydantic v2 `BaseModel`, never `@dataclass`.** Models are strict (`extra="forbid"`).
  Resources that may be created or reused are unions discriminated by `kind`.
- **`X | None`, never `Optional[X]`.** 88-character lines, ruff and mypy strict clean.
- **No secret values in configuration.** Secrets are `{secret, key}` references to
  Secrets Manager.
- **Rules have stable codes**: `S` schema, `T` topology and AWS, `L` licence, `F`
  FinOps, `P` plan. Every rule has a failing-input test in `tests/test_rules.py`.
- **n8n licence terms live in one file**, `config/licensing.py`. Correct them there.
- **`plan.py` is pure**: no AWS calls, no CDK imports. Values known only at deploy time
  are `${placeholders}`.
- **Inject an optional boto3 `session`** in any future AWS-calling function.
- **Prefer stdlib and existing dependencies.** A new runtime dependency is a deliberate
  choice, recorded in `pyproject.toml`.
- **Tag-driven releases.** Use `vYYMMDD` tags. Record changes in `CHANGES.md` under
  `## Unreleased`.
