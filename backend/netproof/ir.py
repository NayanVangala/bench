"""Intermediate representation (IR) for Netproof.

Pydantic v2 data model only. No LLM, no ngspice, no simulation, no
hardcoded demo netlists, no extra dependencies beyond pydantic.

Conventions
-----------
* Node names are strings. ``"0"`` is ground and is required somewhere in
  a netlist, but that requirement is enforced by the pipeline / rules
  layer, NOT by the ``Netlist`` constructor (see ``Netlist.has_ground``).
  Keeping the constructor permissive lets rules report a missing-ground
  finding instead of raising an uncatchable validation error.
* All values are floats in SI units: ohms, farads, volts, amps, watts.
* ``Component.nodes`` is ``[n1, n2]``: for a diode / LED that is
  ``[anode, cathode]``; for a source that is ``[+, -]``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = [
    "ComponentType",
    "Severity",
    "Verdict",
    "Component",
    "Netlist",
    "Finding",
    "Report",
    "SEVERITY_RANK",
]


class ComponentType(StrEnum):
    """Allowed component types."""

    RESISTOR = "resistor"
    CAPACITOR = "capacitor"
    LED = "led"
    DIODE = "diode"
    VOLTAGE_SOURCE = "voltage_source"
    CURRENT_SOURCE = "current_source"


class Severity(StrEnum):
    """Finding severity, worst first."""

    STOP = "STOP"
    WARN = "WARN"
    INFO = "INFO"


class Verdict(StrEnum):
    """Overall report verdict."""

    PASS = "PASS"
    WARN = "WARN"
    STOP = "STOP"


# Sort order for findings: STOP first, then WARN, then INFO.
SEVERITY_RANK: dict[Severity, int] = {
    Severity.STOP: 0,
    Severity.WARN: 1,
    Severity.INFO: 2,
}

# Component types whose ``value`` must be strictly positive (a resistance,
# capacitance, forward voltage, or forward drop of zero / negative is not
# a meaningful part value). Sources may legitimately carry a signed value
# (e.g. a reversed / negative supply), so they are only checked for
# finiteness, which Pydantic enforces for float fields.
_POSITIVE_VALUE_TYPES = frozenset(
    {
        ComponentType.RESISTOR,
        ComponentType.CAPACITOR,
        ComponentType.LED,
        ComponentType.DIODE,
    }
)


class Component(BaseModel):
    """One two-terminal component.

    Attributes
    ----------
    id:
        Component reference, e.g. ``"R1"``, ``"D1"``, ``"V1"``.
    type:
        Component kind (see :class:`ComponentType`).
    value:
        Value in SI units: ohms for a resistor, farads for a capacitor,
        volts for a voltage source / LED forward voltage, amps for a
        current source.
    nodes:
        Exactly two node names, ``[n1, n2]``. Diode / LED:
        ``[anode, cathode]``. Source: ``[+, -]``.
    rating_watts:
        Power rating in watts. Defaults to 0.25 W for resistors when
        omitted; ``None`` for other types unless explicitly given.
    color:
        LED colour, e.g. ``"red"``. Defaults to ``"red"`` for LEDs when
        omitted; ``None`` for other types unless explicitly given.
    """

    model_config = {"frozen": True}

    id: str = Field(min_length=1)
    type: ComponentType
    value: float
    nodes: list[str] = Field(min_length=2, max_length=2)
    rating_watts: float | None = Field(default=None, gt=0)
    color: str | None = Field(default=None, min_length=1)

    @field_validator("id", mode="before")
    @classmethod
    def _strip_id(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("nodes")
    @classmethod
    def _validate_nodes(cls, nodes: list[str]) -> list[str]:
        nodes = [n.strip() for n in nodes]
        if any(not n for n in nodes):
            raise ValueError("node names must be non-empty strings")
        # Identical nodes are ALLOWED here (e.g. ["0", "0"] or
        # ["vcc", "vcc"]): a shorted component/source is an ERC finding
        # reported by rules.py (SHORTED_SOURCE / SHORTED_COMPONENT),
        # not a Pydantic validation error. Length is still exactly 2
        # via the Field(min_length=2, max_length=2) constraint.
        return nodes

    @field_validator("color")
    @classmethod
    def _strip_color(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _apply_type_defaults(self) -> "Component":
        # Frozen model: use object.__setattr__ to apply defaults.
        if self.type is ComponentType.RESISTOR and self.rating_watts is None:
            object.__setattr__(self, "rating_watts", 0.25)
        if self.type is ComponentType.LED and self.color is None:
            object.__setattr__(self, "color", "red")
        if self.type in _POSITIVE_VALUE_TYPES and not self.value > 0:
            raise ValueError(
                f"{self.type.value} value must be > 0, got {self.value}"
            )
        return self


class Netlist(BaseModel):
    """A circuit netlist: a list of components plus an optional title.

    The constructor deliberately does NOT hard-fail when ground node
    ``"0"`` is absent. Use :meth:`has_ground` (or the pipeline / rules
    layer) to check that requirement and report it as a finding.
    """

    model_config = {"frozen": True}

    components: list[Component] = Field(default_factory=list)
    title: str = ""

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @model_validator(mode="after")
    def _check_duplicate_ids(self) -> "Netlist":
        seen: set[str] = set()
        for comp in self.components:
            if comp.id in seen:
                raise ValueError(f"duplicate component id: {comp.id!r}")
            seen.add(comp.id)
        return self

    def has_ground(self) -> bool:
        """Return True if ground node ``"0"`` appears in any component."""
        return any("0" in comp.nodes for comp in self.components)

    def node_names(self) -> set[str]:
        """Return the set of all node names used in the netlist."""
        return {node for comp in self.components for node in comp.nodes}

    def get(self, component_id: str) -> Component | None:
        """Return the component with ``component_id``, or None."""
        for comp in self.components:
            if comp.id == component_id:
                return comp
        return None


class Finding(BaseModel):
    """One check result.

    ``message`` is a single line of plain English and must state the
    measured number and the limit it was checked against, e.g.
    ``"R1 dissipates 0.50 W, over its 0.25 W rating"``.
    """

    model_config = {"frozen": True}

    severity: Severity
    rule_id: str = Field(min_length=1)
    component_ids: list[str] = Field(default_factory=list)
    message: str = Field(min_length=1)

    @field_validator("rule_id", "message", mode="before")
    @classmethod
    def _strip_text(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("message")
    @classmethod
    def _single_line_message(cls, message: str) -> str:
        if "\n" in message or "\r" in message:
            raise ValueError("finding message must be a single line")
        return message

    @field_validator("component_ids")
    @classmethod
    def _strip_component_ids(cls, ids: list[str]) -> list[str]:
        ids = [c.strip() for c in ids]
        if any(not c for c in ids):
            raise ValueError("component_ids must be non-empty strings")
        return ids


class Report(BaseModel):
    """Analysis report for one netlist.

    ``findings`` are always stored sorted by severity, STOP first, then
    WARN, then INFO. ``verdict`` is derived from the findings (any STOP
    finding -> STOP, else any WARN finding -> WARN, else PASS) so the
    verdict can never disagree with the findings list.
    """

    model_config = {"frozen": True}

    verdict: Verdict = Verdict.PASS
    findings: list[Finding] = Field(default_factory=list)
    node_voltages: dict[str, float] = Field(default_factory=dict)
    branch_currents: dict[str, float] = Field(default_factory=dict)
    power: dict[str, float] = Field(default_factory=dict)
    netlist_used: Netlist
    uncertain: list[str] = Field(default_factory=list)

    @field_validator("node_voltages", "branch_currents", "power")
    @classmethod
    def _strip_keys(cls, values: dict[str, float]) -> dict[str, float]:
        return {k.strip(): v for k, v in values.items()}

    @field_validator("uncertain")
    @classmethod
    def _strip_uncertain(cls, items: list[str]) -> list[str]:
        items = [item.strip() for item in items]
        if any(not item for item in items):
            raise ValueError("uncertain entries must be non-empty strings")
        return items

    @model_validator(mode="after")
    def _sort_findings_and_derive_verdict(self) -> "Report":
        ordered = sorted(
            self.findings, key=lambda f: SEVERITY_RANK[f.severity]
        )
        object.__setattr__(self, "findings", ordered)
        if any(f.severity is Severity.STOP for f in ordered):
            verdict = Verdict.STOP
        elif any(f.severity is Severity.WARN for f in ordered):
            verdict = Verdict.WARN
        else:
            verdict = Verdict.PASS
        object.__setattr__(self, "verdict", verdict)
        # Ground is defined as 0 V whenever it appears in the results.
        if "0" in self.node_voltages and self.node_voltages["0"] != 0.0:
            voltages = dict(self.node_voltages)
            voltages["0"] = 0.0
            object.__setattr__(self, "node_voltages", voltages)
        return self
