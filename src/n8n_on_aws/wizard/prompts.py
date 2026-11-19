"""How the assistant asks: a small interface, a console front-end, a scripted one.

The question flow only talks to a :class:`Prompter`, so the same flow runs on a
terminal (:class:`ConsolePrompter`, plain numbered prompts that work over SSH and
in CI logs) and in tests (:class:`ScriptedPrompter`, answers given by key).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import Any, Protocol, TextIO

from pydantic import BaseModel

Check = Callable[[str], str | None]  # returns an error message, or None


class Choice(BaseModel):
    value: str
    label: str


class Aborted(Exception):
    """The user stopped the assistant."""


class Prompter(Protocol):
    def say(self, text: str) -> None: ...

    def text(
        self, key: str, question: str, default: str = "", check: Check | None = None
    ) -> str: ...

    def choose(
        self, key: str, question: str, choices: Sequence[Choice], default: str
    ) -> str: ...

    def confirm(self, key: str, question: str, default: bool) -> bool: ...


class ConsolePrompter:
    """Numbered choices and free text on standard input."""

    def __init__(self, inp: TextIO | None = None, out: TextIO | None = None) -> None:
        self.inp = inp or sys.stdin
        self.out = out or sys.stdout

    def _read(self, prompt: str) -> str:
        self.out.write(prompt)
        self.out.flush()
        line = self.inp.readline()
        if not line:
            raise Aborted("no more input")
        return line.strip()

    def say(self, text: str) -> None:
        print(text, file=self.out)

    def text(
        self, key: str, question: str, default: str = "", check: Check | None = None
    ) -> str:
        hint = f" [{default}]" if default else ""
        while True:
            answer = self._read(f"{question}{hint}: ") or default
            problem = check(answer) if check else None
            if problem is None:
                return answer
            print(f"  ! {problem}", file=self.out)

    def choose(
        self, key: str, question: str, choices: Sequence[Choice], default: str
    ) -> str:
        print(question, file=self.out)
        index = 1
        for number, choice in enumerate(choices, start=1):
            mark = "*" if choice.value == default else " "
            print(f" {mark}{number:>2}. {choice.label}", file=self.out)
            if choice.value == default:
                index = number
        while True:
            answer = self._read(f"Choice [{index}]: ")
            if not answer:
                return choices[index - 1].value
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                return choices[int(answer) - 1].value
            matches = [c for c in choices if c.value == answer]
            if matches:
                return matches[0].value
            print(f"  ! answer 1 to {len(choices)}", file=self.out)

    def confirm(self, key: str, question: str, default: bool) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self._read(f"{question} [{hint}]: ").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            print("  ! answer y or n", file=self.out)


class ScriptedPrompter:
    """Answers by key, for tests and scripted runs; defaults for the rest.

    Every key asked is recorded in ``asked``, and a scripted answer that fails its
    check raises, so a test cannot silently take a wrong branch.
    """

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.asked: list[str] = []
        self.said: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)

    def _get(self, key: str, default: Any) -> Any:
        self.asked.append(key)
        return self.answers.get(key, default)

    def text(
        self, key: str, question: str, default: str = "", check: Check | None = None
    ) -> str:
        answer = str(self._get(key, default))
        problem = check(answer) if check else None
        if problem is not None:
            raise ValueError(f"{key}: {problem}")
        return answer

    def choose(
        self, key: str, question: str, choices: Sequence[Choice], default: str
    ) -> str:
        answer = str(self._get(key, default))
        if answer not in {c.value for c in choices}:
            raise ValueError(f"{key}: {answer!r} is not one of the choices")
        return answer

    def confirm(self, key: str, question: str, default: bool) -> bool:
        return bool(self._get(key, default))
