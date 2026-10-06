"""Service-level airline reference data (alliance membership). Not user configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

DATA_FILE = Path(__file__).parent / "data" / "airlines.yaml"
# "LH 419", "UA8826", "F9 4355", "3U 8888"; IATA codes are never two digits.
_CODE = re.compile(r"^([A-Z]{2}|[A-Z]\d|\d[A-Z])\s*\d")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


@dataclass(frozen=True)
class AirlineReference:
    as_of: str
    alliance_by_code: dict[str, str]  # code -> alliance name (active members only)
    code_by_name: dict[str, str]  # normalized name -> code
    suspended: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_dict(cls, data: dict) -> "AirlineReference":
        alliance_by_code: dict[str, str] = {}
        code_by_name: dict[str, str] = {}

        def add_names(code: str, names: list[str]) -> None:
            for n in [code, *names]:
                key = _norm(n)
                if code_by_name.get(key, code) != code:
                    raise ValueError(f"airline name {n!r} maps to both {code_by_name[key]} and {code}")
                code_by_name[key] = code

        for alliance, members in data["alliances"].items():
            for code, names in members.items():
                if code in alliance_by_code:
                    raise ValueError(f"{code} listed in both {alliance_by_code[code]} and {alliance}")
                alliance_by_code[code] = alliance
                add_names(code, names)
        for section in ("suspended", "unaligned"):
            for code, names in (data.get(section) or {}).items():
                if code in alliance_by_code:
                    raise ValueError(f"{code} is in an alliance and also under {section}")
                add_names(code, names)
        return cls(str(data["as_of"]), alliance_by_code, code_by_name,
                   frozenset((data.get("suspended") or {}).keys()))

    def resolve(self, name_or_code: str) -> Optional[str]:
        """IATA code for an airline name ("Tap Air Portugal") or code ("TP")."""
        return self.code_by_name.get(_norm(name_or_code))

    def alliance_of(self, name_or_code: str) -> Optional[str]:
        code = self.resolve(name_or_code)
        return self.alliance_by_code.get(code) if code else None

    @staticmethod
    def code_from_flight_number(flight_number: str) -> Optional[str]:
        m = _CODE.match(flight_number.strip().upper())
        return m.group(1) if m else None

    def code_for_segment(self, airline: str, flight_number: str) -> Optional[str]:
        return self.code_from_flight_number(flight_number) or self.resolve(airline)


@lru_cache(maxsize=None)
def load_reference(path: str | Path = DATA_FILE) -> AirlineReference:
    return AirlineReference.from_dict(yaml.safe_load(Path(path).read_text()))
