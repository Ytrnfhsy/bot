"""Персонажі оболонки: однакова поведінка, різний вигляд."""

from __future__ import annotations

from .dog import DogWidget
from .robot import RobotWidget

CHARACTERS: dict[str, tuple[str, type[RobotWidget]]] = {
    "robot": ("🤖 Робот", RobotWidget),
    "dog": ("🐶 Такса", DogWidget),
}


def make_character(name: str, scale: float, parent=None) -> RobotWidget:
    _title, cls = CHARACTERS.get(name, CHARACTERS["robot"])
    return cls(scale, parent)
