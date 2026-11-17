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
TRIANGLE = PatternTemplate(
    "TRIANGLE",
    ("0", "A", "B", "C", "D", "E"),
    {2: "B_DEVELOPING", 3: "C_DEVELOPING", 4: "D_DEVELOPING", 5: "E_DEVELOPING"},
    6,
)
FLAT = PatternTemplate(
    "FLAT",
    ("0", "A", "B", "C"),
    {2: "B_DEVELOPING", 3: "C_DEVELOPING"},
    4,
)
DIAGONAL = PatternTemplate(
    "DIAGONAL",
    ("0", "1", "2", "3", "4", "5"),
    {2: "W2_DEVELOPING", 3: "W3_DEVELOPING", 4: "W4_DEVELOPING", 5: "W5_DEVELOPING"},
    6,
)
WXY = PatternTemplate(
    "WXY",
    ("0", "W", "X", "Y"),
    {2: "X_DEVELOPING", 3: "Y_DEVELOPING"},
    4,
)
TEMPLATES = (IMPULSE, ZIGZAG, TRIANGLE, FLAT, DIAGONAL, WXY)
