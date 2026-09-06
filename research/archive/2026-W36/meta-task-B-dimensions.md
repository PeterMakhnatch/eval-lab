---
source_url: https://arxiv.org/abs/2607.27929
source_type: paper
retrieved: 2026-08-19
license_note: "CC BY 4.0 (arXiv HTML states License: CC BY 4.0) — verbatim-quotable with attribution"
status: raw
feeds:
  - library/curated/standards/meta-task/B-dimensions.md
---

# Meta-Task — Appendix B: diversity control dimensions (39 x 10 x 4)

Appendix ref: B (B.1 category, B.2 scenario, B.3 difficulty) of arXiv:2607.27929v1.
Source: https://arxiv.org/html/2607.27929v1. Tables extracted from HTML; the three representative
specifications are inline SVG figures, extracted verbatim below.

**Why this matters here.** Their orthogonal dimensions give 39x10x4 = 1,560 base
combinations, and the *shape* of each dimension spec is what we can reuse: a
category carries "Task Pattern Ideas / Skills & Tools / Possible Input Data /
Verification Approaches", a scenario carries writing-style principles with a good
example, a difficulty level carries MUST-meet criteria plus an explicit
anti-pattern list. That anti-pattern discipline is the same instinct as Peter's
taxonomy-of-bad-tasks in `drive-evals-benchmarks.md`; STANDARDS should merge them
rather than keep two vocabularies. Note their own practice, stated in B.3: they
"primarily use the hard and extreme levels for task generation."

## B.1 — all 39 categories (Table 6, verbatim)

| # | Category | # | Category |
|---|---|---|---|
| 1 | Algorithm Design | 21 | Git Operations |
| 2 | API Design | 22 | Incident Response |
| 3 | Bioinformatics | 23 | Message Queues |
| 4 | Code Audit | 24 | ML Training |
| 5 | Code Golf | 25 | Monitoring |
| 6 | Code Migration | 26 | Networking |
| 7 | Compilation & Build | 27 | Optimization |
| 8 | Cryptanalysis | 28 | Physics & Simulation |
| 9 | Database Systems | 29 | Puzzle & Investigation |
| 10 | Data Processing | 30 | Scientific Computing |
| 11 | Debugging | 31 | Security & CTF |
| 12 | DevOps & CI/CD | 32 | Shell Scripting |
| 13 | Distributed ML | 33 | Software Engineering |
| 14 | Distributed Systems | 34 | System Administration |
| 15 | Env. & Dependencies | 35 | System Orchestration |
| 16 | File Operations | 36 | Testing & QA |
| 17 | Financial Engineering | 37 | Text Processing |
| 18 | FPGA & Hardware | 38 | Video & Multimedia |
| 19 | Functional Languages | 39 | Web Development |
| 20 | Games & Simulations |  |  |

### Representative category specification, verbatim (Figure in B.1)

```text
Category: Database and SQL
Tasks involving database operations, SQL queries, schema design, query optimization, and database administration.
The examples below are for inspiration only. Be creative and design your own unique task!
Task Pattern Ideas:
- Write and optimize complex SQL queries
- Design database schemas for specific requirements
- Implement database migrations and schema evolution
- Implement stored procedures or triggers
- Cross-database data migration (MySQL to PostgreSQL, etc.)
- Implement full-text search functionality
- Time-series data management and queries
- Implement database connection pooling
- Data integrity validation and cleanup
- Implement caching strategies with database backends
Skills & Tools (tag suggestions): Core: database, sql, data-management, query-optimization; Databases: sqlite, postgresql, mysql, mongodb, redis; Languages: sql; Tools: sqlite3, psql, mysql-client, pgcli
Possible Input Data (be creative with environment files!): SQLite database files (.sqlite, .db); SQL dump files (.sql) with schema and data; Slow query logs for optimization tasks; Schema definition files (DDL scripts); CSV/JSON data for import tasks; Database configuration files; Index statistics and query plans
Verification Approaches: Verify query results match expected output; Check query performance (execution time, explain plan); Validate schema correctness (constraints, relationships); Test data integrity after migrations; Verify index usage in query plans; Check transaction handling (ACID compliance)
Remember: These are just starting points. Create database challenges that test SQL proficiency, query optimization skills, and understanding of database internals!
```

## B.2 — all 10 scenario styles (Table 7, verbatim)

| Scenario | Description |
|---|---|
| Minimal | Ultra-brief 1–3 sentences |
| Structured | Well-organized request with goal and context |
| Narrative | Workplace story with characters |
| Emergency | Urgent time-pressure style |
| Creative | Unusual or playful framing |
| Casual | Informal conversational tone |
| Follow-up | Continuation with new requirements |
| Multi-request | Multiple sub-tasks bundled |
| Specification | Formal specification style |
| Terminal Dump | Starts with pasted terminal output |

### Representative scenario specification, verbatim (Figure in B.2)

```text
Scenario: Narrative
For the instruction.md you generate, use a scenario-based narrative style:
Key principles:
- Tell a story – Set up a realistic workplace situation
- Use characters – “Your colleague Marcus”, “The intern”, “Sarah from the ops team”
- Explain the situation – What’s happening and what needs to be done
- Natural flow – Don’t use formal sections, write as connected paragraphs
- Embedded requirements – Weave technical details into the narrative
Good example:
The data science team has been collecting sensor readings from 200 IoT devices across three warehouses. The raw data lives in /app/data/sensors/ -- one Parquet file per day, going back 90 days. Lisa from the analytics team needs a consolidated dashboard-ready dataset by end of week. The problem is that the devices use three different firmware versions, each with slightly different data schemas (some use Celsius, others Fahrenheit; timestamp formats vary). Your job: build a pipeline that normalizes all the data into a consistent schema, identifies anomalous readings, and produces a clean daily-aggregated dataset at /app/output/dashboard.parquet.
Remember: Make it feel like a real workplace situation – not a formal specification, but a story with a clear deliverable.
```

## B.3 — four difficulty levels (Table 8, verbatim)

| Level | Skill Scope | Solution Characteristics |
|---|---|---|
| Easy | Single core tool/technique | Clear input -> process -> output workflow |
| Medium | 2–3 tools combined with judgment calls | Multi-step; agent must choose approach based on discovery |
| Hard | Specialized domain expertise beyond basic programming | Non-obvious path; difficulty from real materials, not artificial constraints |
| Extreme | Deep knowledge requiring active research of provided materials | Discovery-driven; 3+ chained non-obvious insights |

### Representative difficulty specification, verbatim (Figure in B.3)

```text
Difficulty: Hard
Your task MUST meet ALL of the following criteria to qualify as HARD difficulty:
1. Specialized Knowledge Required: The task must require domain expertise that goes beyond basic programming (e.g., cryptographic algorithms, compiler internals, signal processing, bioinformatics). A general-purpose developer should NOT be able to solve it without learning new concepts.
2. Non-Obvious Solution Path: The solution must NOT be a straightforward “read input  ->  process  ->  write output” pipeline. Require creative problem-solving, algorithm design, or reverse engineering. Include at least one step where the solver must discover or infer something not explicitly stated.
3. Difficulty from the Materials, Not Artificial Constraints: The challenge should come from the inherent complexity of the real materials. Do NOT make tasks hard by just adding arbitrary time limits or format restrictions on top of a simple problem.
4. Multi-Stage Verification: Tests must verify multiple intermediate or final artifacts. Include at least 3 distinct verification checks. Verify both correctness AND constraints (performance, size, format).
5. Real-World Complexity: Model realistic scenarios with edge cases and error conditions. Include at least one “trap” that a naive implementation would fail on. The task should take an expert 30+ minutes to solve.
Anti-patterns to AVOID: Simple CRUD operations; Tasks solvable with a single library call; Problems with obvious, linear solutions; Basic ML training (MNIST-level tasks); Making a simple task “hard” by adding artificial constraints.
```
