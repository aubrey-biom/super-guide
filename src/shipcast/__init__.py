"""shipcast: demand and shipment forecaster for Biom retail channels (v1: Target).

Turns Target's own replenishment signals (BigQuery project biom-reporting-s26,
read through bullseye's logical tables) into expected PO units by TCIN by fiscal
week, graded by measured accuracy, plus a monthly consumption view for S&OP and
an ability-to-ship layer against the RDZ inventory sheet (Biom's own distribution center).
"""

__version__ = "0.1.0"
