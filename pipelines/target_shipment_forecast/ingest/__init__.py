"""Ingestion jobs that land hand-maintained inputs in BigQuery as append-only snapshots.

The forecast engine itself reads BigQuery only. Anything a person maintains outside the
warehouse (today: the channel owner's Brick & Mortar Master Forecast) gets here first,
through a job that runs as a service account with its own access grant, and the engine
then reads the snapshot as-of the run date. The pattern is the RDZ inventory pipeline's.
"""
