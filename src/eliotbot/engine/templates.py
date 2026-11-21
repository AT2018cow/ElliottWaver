"""The implemented template registry; unsupported patterns are never auto-renamed."""

from dataclasses import dataclass

from eliotbot.schemas.wave import Pattern


@dataclass(frozen=True)
class PatternTemplate:
    name: Pattern
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

_MOTIVE = ("IMPULSE", "DIAGONAL")
_CORRECTION = ("ZIGZAG_ABC", "FLAT", "WXY")
_WITH_TRIANGLE = (*_CORRECTION, "TRIANGLE")
# Four legs are three-wave corrections. A rare E-leg nested triangle is
# permitted only as a provisional parent link; parents.py never marks it VERIFIED.
_TRIANGLE_LEG = ("ZIGZAG_ABC", "FLAT", "WXY")
_CHILD_TEMPLATES: dict[Pattern, tuple[tuple[Pattern, ...], ...]] = {
    "IMPULSE": (_MOTIVE, _CORRECTION, ("IMPULSE",), _WITH_TRIANGLE, _MOTIVE),
    "ZIGZAG_ABC": (_MOTIVE, _WITH_TRIANGLE, _MOTIVE),
    "FLAT": (_CORRECTION, _WITH_TRIANGLE, _MOTIVE),
    "TRIANGLE": (*(_TRIANGLE_LEG,) * 4, (*_TRIANGLE_LEG, "TRIANGLE")),
    "WXY": (("ZIGZAG_ABC",), _WITH_TRIANGLE, ("ZIGZAG_ABC",)),
}


def allowed_child_templates_advanced(
    parent: Pattern, leg_index: int, variant: str | None = None
) -> tuple[Pattern, ...]:
    """Advanced profiles: WXY combinations and diagonal 3-3-3-3-3/5-3-5-3-5."""
    if parent == "WXY" and variant != "DOUBLE_ZIGZAG":
        return (
            (
                ("ZIGZAG_ABC", "FLAT"),
                ("ZIGZAG_ABC", "FLAT", "TRIANGLE", "WXY"),
                ("ZIGZAG_ABC", "FLAT", "TRIANGLE"),
            )[leg_index]
            if 0 <= leg_index < 3
            else ()
        )
    if parent == "DIAGONAL":
        if not 0 <= leg_index < 5:
            return ()
        # Ending diagonals are 3-3-3-3-3. Leading diagonals accept both the
        # classic 3-3-3-3-3 and the 5-3-5-3-5 subdivision.
        if variant == "LEADING_DIAGONAL" and leg_index in {0, 2, 4}:
            return (*_MOTIVE, *_CORRECTION)
        return _CORRECTION
    return allowed_child_templates(parent, leg_index)


def allowed_child_templates(parent: Pattern, leg_index: int) -> tuple[Pattern, ...]:
    """Return the templates for a direct child, indexed from the first parent leg.

    The endpoint-only DIAGONAL does not identify the leading or ending subtype,
    so its internal legs have no certification profile.
    """
    positions = _CHILD_TEMPLATES.get(parent)
    if positions is None or not 0 <= leg_index < len(positions):
        return ()
    return positions[leg_index]


_CORRECTION_POSITIONS = {
    ("IMPULSE", "2"),
    ("IMPULSE", "4"),
    ("ZIGZAG_ABC", "B"),
    ("FLAT", "B"),
    ("TRIANGLE", "B"),
    ("TRIANGLE", "D"),
    ("WXY", "X"),
    ("WXY", "Y"),
}
_DIAGONAL_POSITIONS = {
    ("IMPULSE", "1"),
    ("IMPULSE", "3"),
    ("IMPULSE", "5"),
    ("ZIGZAG_ABC", "A"),
    ("ZIGZAG_ABC", "C"),
    ("FLAT", "A"),
    ("FLAT", "C"),
    ("WXY", "W"),
    ("WXY", "Y"),
}
VARIANT_PARENT_POSITIONS: dict[str, set[tuple[Pattern, str]]] = {
    "REGULAR_FLAT": _CORRECTION_POSITIONS,
    "EXPANDED_FLAT": _CORRECTION_POSITIONS,
    "RUNNING_FLAT": {
        ("IMPULSE", "2"),
        ("ZIGZAG_ABC", "B"),
        ("FLAT", "B"),
        ("WXY", "X"),
    },
    "BARRIER_TRIANGLE": {
        ("IMPULSE", "4"),
        ("ZIGZAG_ABC", "B"),
        ("FLAT", "B"),
        ("WXY", "X"),
        ("WXY", "Y"),
    },
    "CONTRACTING_DIAGONAL": _DIAGONAL_POSITIONS,
    "EXPANDING_DIAGONAL": _DIAGONAL_POSITIONS,
    "LEADING_DIAGONAL": {
        ("IMPULSE", "1"),
        ("ZIGZAG_ABC", "A"),
    },
    "ENDING_DIAGONAL": {
        ("IMPULSE", "5"),
        ("ZIGZAG_ABC", "C"),
        ("FLAT", "C"),
    },
    "TRUNCATED_FIFTH": {
        ("IMPULSE", "5"),
        ("ZIGZAG_ABC", "C"),
        ("FLAT", "C"),
        ("WXY", "Y"),
    },
    "DOUBLE_ZIGZAG": _CORRECTION_POSITIONS,
    "DOUBLE_THREE": _CORRECTION_POSITIONS,
}


def variant_allowed_in_parent(
    variant: str | None,
    parent_pattern: Pattern | None,
    parent_wave_label: str | None,
) -> bool:
    """Check the parent-position matrix; unknown parents keep generic geometry."""
    if variant is None:
        return True
    if parent_pattern is None or parent_wave_label is None:
        return True
    positions = VARIANT_PARENT_POSITIONS.get(variant)
    if positions is None:
        return True
    return (parent_pattern, parent_wave_label) in positions
