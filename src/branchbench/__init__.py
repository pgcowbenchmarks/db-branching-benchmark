"""Database provisioning-cost benchmark harness.

Measures the per-cycle cost of provisioning a fresh, production-shaped database
for an automated-test inner loop, across a matrix of provisioning strategies and
data scales. The central question: which strategies keep provisioning cost flat
as the dataset grows (copy-on-write branching) versus rising with data size
(fresh fixture, restore, copy-based branch)?

Engines: SQLite (in-memory floor), PostgreSQL (local Docker, RDS PostgreSQL),
Aurora PostgreSQL (fast clone) and Neon (serverless Postgres copy-on-write
branching -- the technology Databricks Lakebase is built on). All strategies at a given scale seed from one
deterministically generated, production-shaped parent so only the provisioning
mechanism varies along a shared data-size axis.
"""

__version__ = "0.1.0"
