"""Fiber link checker: a deterministic optical power budget + fixed rules.

The fiber equivalent of the circuit check: the LLM may propose a link,
but the verdict comes only from this calculation. A link is an ordered
path from one laser to one detector:

    laser -> fiber -> connector -> splitter -> ... -> detector

Every element has a loss in dB (defaults below, or ``loss_db`` to
override); the power after each element is the laser power minus the
running loss. Rules then check the received power against the detector,
connector polish compatibility, wavelength ranges and laser safety.
Findings use the same ``ir.Finding`` shape as circuit checks, and the
verdict is derived the same way (any STOP -> STOP, else any WARN -> WARN).
"""
from __future__ import annotations

import math
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .ir import Finding, Severity

ElementType = Literal["laser", "fiber", "connector", "splice", "splitter", "attenuator", "isolator", "detector"]

# Typical losses (dB). Lab-grade values; override per element with loss_db.
CONNECTOR_LOSS_DB = 0.3      # one mated connector pair
SPLICE_LOSS_DB = {"fusion": 0.05, "mechanical": 0.3}
SPLITTER_EXCESS_DB = 0.5     # on top of the ideal 10*log10(N) split
ISOLATOR_LOSS_DB = 0.6
SMF_CUTOFF_NM = 1260         # SMF-28: single-mode above this wavelength
LINK_MARGIN_WARN_DB = 3.0
LASER_SAFETY_DBM = 7.0       # ~5 mW: Class 3R/3B boundary


def fiber_attenuation_db_per_km(fiber_type: str, wavelength_nm: float) -> float:
    """Typical attenuation for SMF-28-class and OM3-class multimode fiber."""
    if fiber_type == "mmf":
        return 3.0 if wavelength_nm < 1000 else 1.0
    if wavelength_nm >= 1500:
        return 0.20
    if wavelength_nm >= 1260:
        return 0.35
    if wavelength_nm >= 800:
        return 2.0
    return 8.0


class FiberElement(BaseModel):
    id: str = Field(min_length=1)
    type: ElementType
    # laser
    wavelength_nm: Optional[float] = None
    power_dbm: Optional[float] = None
    # fiber
    length_m: Optional[float] = None
    fiber_type: Optional[Literal["smf", "mmf"]] = None
    # connector (mated pair): polish of each side
    polish_a: Optional[Literal["PC", "UPC", "APC"]] = None
    polish_b: Optional[Literal["PC", "UPC", "APC"]] = None
    # splice
    splice_kind: Optional[Literal["fusion", "mechanical"]] = None
    # splitter 1xN (the link follows one output)
    ports: Optional[int] = Field(default=None, ge=2, le=128)
    # attenuator value, or an override for any passive element
    loss_db: Optional[float] = Field(default=None, ge=0)
    # detector
    sensitivity_dbm: Optional[float] = None
    max_dbm: Optional[float] = None
    min_nm: Optional[float] = None
    max_nm: Optional[float] = None


class FiberLink(BaseModel):
    title: str = ""
    elements: list[FiberElement] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> "FiberLink":
        ids = [e.id for e in self.elements]
        if len(ids) != len(set(ids)):
            raise ValueError("element ids must be unique")
        return self


def dbm_to_mw(dbm: float) -> float:
    return 10 ** (dbm / 10)


def element_loss_db(e: FiberElement, wavelength_nm: float) -> float:
    if e.type in ("laser", "detector"):
        return 0.0
    if e.loss_db is not None:
        return e.loss_db
    if e.type == "fiber":
        km = (e.length_m or 1.0) / 1000
        return fiber_attenuation_db_per_km(e.fiber_type or "smf", wavelength_nm) * km
    if e.type == "connector":
        return CONNECTOR_LOSS_DB
    if e.type == "splice":
        return SPLICE_LOSS_DB[e.splice_kind or "fusion"]
    if e.type == "splitter":
        return 10 * math.log10(e.ports or 2) + SPLITTER_EXCESS_DB
    if e.type == "isolator":
        return ISOLATOR_LOSS_DB
    if e.type == "attenuator":
        return 10.0
    return 0.0


def _detector(e: FiberElement) -> FiberElement:
    """Detector with defaults filled in (InGaAs power-meter-class)."""
    return e.model_copy(update={
        "sensitivity_dbm": e.sensitivity_dbm if e.sensitivity_dbm is not None else -40.0,
        "max_dbm": e.max_dbm if e.max_dbm is not None else 3.0,
        "min_nm": e.min_nm if e.min_nm is not None else 800.0,
        "max_nm": e.max_nm if e.max_nm is not None else 1700.0,
    })


def check_link(link: FiberLink | dict) -> dict:
    """Power budget + rules -> report dict (verdict, findings, trace, ...)."""
    if isinstance(link, dict):
        link = FiberLink.model_validate(link)
    els = link.elements
    findings: list[Finding] = []

    def add(sev: Severity, rule: str, ids: list[str], msg: str) -> None:
        findings.append(Finding(severity=sev, rule_id=rule, component_ids=ids, message=msg))

    lasers = [e for e in els if e.type == "laser"]
    detectors = [e for e in els if e.type == "detector"]
    if len(lasers) != 1 or els[0].type != "laser":
        add(Severity.STOP, "LINK_NO_SOURCE", [e.id for e in lasers],
            f"A link must start with exactly one laser; found {len(lasers)} laser(s), first element is {els[0].type}.")
    if len(detectors) != 1 or els[-1].type != "detector":
        add(Severity.STOP, "LINK_NO_DETECTOR", [e.id for e in detectors],
            f"A link must end with exactly one detector; found {len(detectors)} detector(s), last element is {els[-1].type}.")

    laser = lasers[0] if lasers else None
    wl = (laser.wavelength_nm if laser and laser.wavelength_nm else 1550.0)
    p = (laser.power_dbm if laser and laser.power_dbm is not None else 0.0)
    trace = []
    total_loss = 0.0
    for e in els:
        loss = element_loss_db(e, wl)
        total_loss += loss
        p -= loss
        trace.append({"id": e.id, "type": e.type, "loss_db": round(loss, 3),
                      "power_dbm": round(p, 3), "power_mw": dbm_to_mw(p)})

    # Connector polish: APC (angled) never mates with PC/UPC (flat).
    for e in els:
        if e.type == "connector" and e.polish_a and e.polish_b:
            if (e.polish_a == "APC") != (e.polish_b == "APC"):
                add(Severity.STOP, "CONNECTOR_POLISH_MISMATCH", [e.id],
                    f"{e.id} mates {e.polish_a} to {e.polish_b}: an angled (APC) end against a flat (PC/UPC) end "
                    f"leaves an air gap with high loss and can damage both end faces.")

    # Single-mode fiber below its cutoff wavelength carries several modes.
    for e in els:
        if e.type == "fiber" and (e.fiber_type or "smf") == "smf" and wl < SMF_CUTOFF_NM:
            add(Severity.WARN, "FIBER_BELOW_CUTOFF", [e.id],
                f"{e.id} is single-mode fiber used at {wl:g} nm, below its ~{SMF_CUTOFF_NM} nm cutoff: "
                f"it is not single-mode there and loses {fiber_attenuation_db_per_km('smf', wl):g} dB/km.")

    if laser and laser.power_dbm is not None and laser.power_dbm >= LASER_SAFETY_DBM:
        add(Severity.WARN, "LASER_SAFETY", [laser.id],
            f"{laser.id} emits {laser.power_dbm:g} dBm ({dbm_to_mw(laser.power_dbm):.1f} mW), at or above about 5 mW "
            f"(Class 3B range): never look into a fiber end, use laser safety glasses rated for {wl:g} nm.")

    if detectors and els[-1].type == "detector":
        d = _detector(detectors[0])
        rx = trace[-1]["power_dbm"]
        if not (d.min_nm <= wl <= d.max_nm):
            add(Severity.STOP, "DETECTOR_WAVELENGTH", [d.id],
                f"{d.id} covers {d.min_nm:g}-{d.max_nm:g} nm but the laser is {wl:g} nm: it will not detect this light.")
        margin = rx - d.sensitivity_dbm
        if rx > d.max_dbm:
            add(Severity.STOP, "DETECTOR_OVERLOAD", [d.id],
                f"{d.id} receives {rx:.1f} dBm ({dbm_to_mw(rx):.2f} mW), over its {d.max_dbm:g} dBm maximum input: add attenuation.")
        elif rx > d.max_dbm - LINK_MARGIN_WARN_DB:
            add(Severity.WARN, "DETECTOR_OVERLOAD", [d.id],
                f"{d.id} receives {rx:.1f} dBm, within {LINK_MARGIN_WARN_DB:g} dB of its {d.max_dbm:g} dBm maximum input.")
        if margin < 0:
            add(Severity.STOP, "LINK_MARGIN", [d.id],
                f"{d.id} receives {rx:.1f} dBm, below its {d.sensitivity_dbm:g} dBm sensitivity "
                f"({-margin:.1f} dB short): the link will not work.")
        elif margin < LINK_MARGIN_WARN_DB:
            add(Severity.WARN, "LINK_MARGIN", [d.id],
                f"{d.id} receives {rx:.1f} dBm, only {margin:.1f} dB above its {d.sensitivity_dbm:g} dBm sensitivity "
                f"(want at least {LINK_MARGIN_WARN_DB:g} dB margin for dirty connectors and aging).")

    order = {Severity.STOP: 0, Severity.WARN: 1, Severity.INFO: 2}
    findings.sort(key=lambda f: order[f.severity])
    verdict = ("STOP" if any(f.severity is Severity.STOP for f in findings)
               else "WARN" if any(f.severity is Severity.WARN for f in findings) else "PASS")
    return {
        "domain": "fiber",
        "verdict": verdict,
        "findings": [f.model_dump(mode="json") for f in findings],
        "trace": trace,
        "wavelength_nm": wl,
        "launch_dbm": laser.power_dbm if laser and laser.power_dbm is not None else 0.0,
        "total_loss_db": round(total_loss, 3),
        "received_dbm": trace[-1]["power_dbm"] if trace else None,
        "link": link.model_dump(mode="json"),
    }
