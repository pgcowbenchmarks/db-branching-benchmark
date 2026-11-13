| Strategy | Engine | 1 GB | 2 GB | 8 GB | 32 GB | 64 GB | 128 GB | 256 GB |
|---|---|---|---|---|---|---|---|---|
| In-memory fake | SQLite (client) | 27.5 | 28.9 | OOM | OOM | OOM | OOM | OOM |
| Fresh fixture | RDS PostgreSQL | 31.4 | 58.4 | 223 | 897 | 1,726 | 3,196 | 7,265 |
| Shared fixture (transactional rollback) | RDS PostgreSQL | 0.336 | 0.393 | 0.386 | 0.399 | 0.593 | 0.608 | n.r. |
| Full copy / restore | RDS PostgreSQL | 67.7 | 162 | 473 | fail | fail | skip | n.r. |
| Copy-based branch | RDS PostgreSQL | 8.5 | 17.2 | 70.5 | 320 | 633 | 1,104 | 2,275 |
| Copy-on-write branch | Neon | 0.388 | 0.378 | 0.429 | 0.400 | 0.381 | 0.398 | 0.389 |
| Copy-on-write clone | Aurora PostgreSQL | 454 | 369 | 353 | 452 | 362 | 461 | 401 |

Medians in seconds at nominal parent sizes. fail = the attempt died before finishing; OOM = out of memory; skip = not attempted because the dump would not fit on local disk; n.r. = not run.
Full restore at 32 GB ran 983 s (a 786 s dump plus 197 s of restore) before the restore died; the 64 GB attempt failed the same way (time not recorded).
Shared fixture (transactional rollback) is a no-isolation baseline: it is flat only because cycles share one database.
