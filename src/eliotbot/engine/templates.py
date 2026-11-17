"""The implemented template registry; unsupported patterns are never auto-renamed."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PatternTemplate:
    name: str
    labels: tuple[str, ...]
    developing_stages: dict[int, str]
    complete_points: int


IMPULSE = PatternTemplate(
    "IMPULSE",
    ("0", "1", "2", "3", "4", "5"),
    {2: "W2_DEVELOPING", 3: "W3_DEVELOPING", 4: "W4_DEVELOPING", 5: "W5_DEVELOPING"},
    6,
)
ZIGZAG = PatternTemplate(
    "ZIGZAG_ABC",
    ("0", "A", "B", "C"),
    {2: "B_DEVELOPING", 3: "C_DEVELOPING"},
    4,
)
TEMPLATES = (IMPULSE, ZIGZAG)
