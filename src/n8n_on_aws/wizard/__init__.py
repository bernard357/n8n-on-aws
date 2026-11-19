"""``n8n-on-aws init``: an assistant that writes a minimal, commented ``n8n.yaml``."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from ..config import LoadResult, resolve
from .emit import prune, render
from .flow import Flow
from .lookups import Lookups
from .prompts import Aborted, ConsolePrompter, Prompter, ScriptedPrompter

__all__ = [
    "Aborted",
    "ConsolePrompter",
    "Draft",
    "Lookups",
    "ScriptedPrompter",
    "from_sample",
    "interview",
]


class Draft(BaseModel):
    """A configuration ready to be written, with its validation result."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    data: dict[str, Any]
    text: str
    result: LoadResult

    @property
    def ok(self) -> bool:
        return self.result.ok


def _draft(
    data: dict[str, Any], comments: dict[str, str], header: list[str], plain: bool
) -> Draft:
    minimal = prune(data)
    return Draft(
        data=minimal,
        text=render(minimal, comments, header, plain),
        result=resolve(minimal),
    )


def interview(ask: Prompter, lookups: Lookups, plain: bool = False) -> Draft:
    """Ask the questions and return the file they lead to."""
    flow = Flow(ask, lookups)
    data = flow.run()
    header = [
        "Written by `n8n-on-aws init`. Only the choices that differ from the",
        f"'{data.get('profile', 'small')}' profile are here; see",
        "docs/research/04-configuration-grammar.md for every other key.",
        "  n8n-on-aws plan -f <this file> --cost",
    ]
    return _draft(data, flow.comments, header, plain)


def from_sample(
    sample: Path, ask: Prompter, lookups: Lookups, plain: bool = False
) -> Draft:
    """Copy a sample, asking only for the name and the region."""
    data = yaml.safe_load(sample.read_text(encoding="utf-8")) or {}
    data = copy.deepcopy(data)
    flow = Flow(ask, lookups)
    flow.identity()
    data["name"] = flow.data["name"]
    data.setdefault("aws", {})["region"] = flow.data["aws"]["region"]
    data.setdefault("n8n", {})["version"] = flow.data["n8n"]["version"]
    header = [
        f"Written by `n8n-on-aws init --from {sample.stem}`.",
        "  n8n-on-aws plan -f <this file> --cost",
    ]
    return Draft(data=data, text=render(data, {}, header, plain), result=resolve(data))
