"""The forecasting model.

Implemented (v1): `plan_anchor` (weekly plan-anchored PO forecast), `grade`
(lead-day grades and confidence labels), `intervals` (leave-one-week-out
bands), `consumption` (monthly consumption model with a calibrated inventory
controller). Still typed stubs for v1.5: `gate` (signal admission as a
reusable function; the pipeline applies the rule inline), `casepack`
(rounding derived rows), `supply_ledger` (ability to ship).
"""

from __future__ import annotations

from shipcast.model import casepack, consumption, gate, grade, intervals, plan_anchor, supply_ledger

__all__ = ["casepack", "consumption", "gate", "grade", "intervals", "plan_anchor", "supply_ledger"]
