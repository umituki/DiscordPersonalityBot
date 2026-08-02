# Repairing an existing database

If YUI has already been running, fixing the code does not fix her. The database
still holds a Genesis that never produced any psychology — and the same
database holds every real conversation you have had with her, which is why the
answer is never "delete it and start again".

This is the procedure of patch spec 23. Three commands, three decisions, and
only the last one touches the database your history is in.

## 1. Diagnose

```
python -m app.main diagnose
```

Read-only. It prints `genesis_health` and what is missing:

| verdict | meaning |
| --- | --- |
| `healthy` | the Genesis under this database holds up; nothing to do |
| `no_genesis` | nothing has booted yet; there is nothing to repair |
| `needs_rebuild` | it booted from a broken Genesis, and the original seed is still there |
| `legacy_invalid` | it booted from a broken Genesis, and the seed to rebuild from is gone |

Findings are stated in terms of what is missing — `appraisal_never_ran`,
`simulated_episodic_path_inactive`, `historical_knowledge_empty`,
`periodic_consolidation_missing`. Exit code is 0 when sound, 1 when not.

## 2. Plan (dry run)

```
python -m app.main repair
```

Writes nothing. It reports the shadow path it would use, how many real Discord
events would be replayed, and anything blocking the repair — a missing seed, a
history that is not in chronological order, a shadow file left over from a
previous attempt.

Fix the blockers before going further. A blocked repair is not a repair to
force.

## 3. Rebuild into a shadow database

```
python -m app.main repair --apply
```

Production is still not written to. In order:

1. a verified backup is taken (the repair stops if it does not verify)
2. the original temperament seed and life scaffold are read
3. the patched engine lives the same life again, into `data/shadow/`
4. the strengthened FIRST BOOT audits decide whether that life is one worth keeping
5. every real Discord event recorded after the original FIRST BOOT is extracted, oldest first
6. they are replayed into the shadow database
7. nothing is sent — the replay builds no Discord gateway and no conversation service
8. what YUI said stays exactly what she said; the text comes out of the archive
9. no reply is regenerated
10. the state consequences of those real events are rebuilt on the repaired past
11. integrity, counts and chronology are compared

The output ends with `verification.clean`. If it is `false`, read
`verification.findings` and `audits_failed` — the shadow is not a database to
switch to, and nothing has changed.

## 4. Switch

```
python -m app.main repair --apply --switch "REPLACE PRODUCTION"
```

Refused unless the rebuild booted and the verification came back clean. The
confirmation string is required and exact.

The old database is **renamed**, not removed:

```
data/yui.db          ← the repaired database
data/yui.rollback-<timestamp>.db   ← what was there before
```

The WAL and SHM siblings move with it, so the rollback is a whole database
rather than most of one.

## 5. Rolling back

The rollback file is the original. To go back to it, stop YUI and swap the two
files, or call `RepairService.rollback` with the result of the switch — it puts
the original back and keeps the repaired one at `data/yui.repaired.db`.

## What this will not do

- **Delete anything.** No command here removes a row or a file. A full reset is
  your choice and is deliberately not implemented.
- **Re-send a message.** The replay cannot: it never constructs a gateway.
- **Rebuild from a different seed.** If the original seed is gone the repair
  refuses, because a rebuild from a new one is a different person wearing the
  same history.
- **Switch to something that did not pass.** The audits are the gate, and a
  shadow that failed them cannot be promoted.

## Before you start

- Stop YUI. The switch moves the database file.
- Check `data/shadow/` is empty, or move a previous attempt aside.
- Have `config/knowledge/` populated for the period you are simulating —
  a rebuild with an empty knowledge layer fails its own audits (see
  `config/knowledge/README.md`).
- Ollama has to be reachable: the rebuild summarises episodes and narrates
  significant experiences. Without it the rebuilt Genesis produces no memories
  and the audits will refuse it — correctly.
