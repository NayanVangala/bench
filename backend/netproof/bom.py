"""Bill of materials (BOM) export for Netproof.

Pure functions only. No LLM, no ngspice, no simulation, no I/O.

One public function: :func:`netlist_to_bom_csv`, which renders a
:class:`~netproof.ir.Netlist` as CSV with header
``id,type,value,rating,qty``. Rows are grouped by identical
``type`` + ``value``: identical parts collapse to a single row whose
``qty`` is the count in the group and whose ``id`` is the first
component id in that group (first-seen order is preserved).

The SPICE text export (``netlist_to_spice_text``) lives in ``spice.py``
and is deliberately NOT duplicated or imported here: this module has
no use for it, and the download endpoint can import it from ``spice``
itself.
"""

from __future__ import annotations

import csv
import io

from .ir import Netlist

__all__ = ["netlist_to_bom_csv"]

_HEADER = ("id", "type", "value", "rating", "qty")


def _format_number(value: float) -> str:
    """Format an SI float compactly and round-trippably (``470.0`` -> ``"470"``)."""
    return format(float(value), "g")


def netlist_to_bom_csv(netlist: Netlist | dict) -> str:
    """Render ``netlist`` as a BOM CSV string.

    Parameters
    ----------
    netlist:
        A :class:`~netproof.ir.Netlist`, or a plain ``dict`` in the same
        shape (e.g. a parsed JSON fixture), which is validated into a
        ``Netlist`` first.

    Returns
    -------
    str
        CSV text with header ``id,type,value,rating,qty`` and one row
        per distinct ``(type, value)`` group, in first-seen order.
        ``rating`` is the group's power rating in watts (empty when the
        components carry none); when grouped components disagree on
        rating, the first-seen rating is shown. ``qty`` is the number
        of components in the group. An empty netlist yields the header
        alone.
    """
    if isinstance(netlist, dict):
        netlist = Netlist.model_validate(netlist)

    # Group by (type, value) only, preserving first-seen order.
    # value is a float, so group on the exact stored float, not on a
    # rounded display string: 470.0 and 470.0000001 are different parts.
    groups: dict[tuple[str, float], list] = {}
    for comp in netlist.components:
        key = (comp.type.value, comp.value)
        groups.setdefault(key, []).append(comp)

    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_HEADER)
    for (type_value, value), comps in groups.items():
        first = comps[0]
        rating = (
            _format_number(first.rating_watts)
            if first.rating_watts is not None
            else ""
        )
        writer.writerow(
            [
                first.id,
                type_value,
                _format_number(value),
                rating,
                len(comps),
            ]
        )
    return buf.getvalue()
