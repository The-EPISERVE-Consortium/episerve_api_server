# Frozen benchmark definitions

At finalization, one lakeFS commit in the configured `benchmarks` repository contains:

```text
<sharded-QID>/<QID>.fdo.json
<sharded-QID>/components/input.parquet
<sharded-QID>/components/reference.parquet
<sharded-QID>/components/benchmark-definition.json
```

The FDO lists the descriptor as `application/json` and records its SHA-256 checksum
in `profile.episerveBenchmark.files.definition`. The API exposes its versioned
download URL under `GET /benchmarks/{id}` → `generated_files.definition.url`.
MariaDB records the manifest only after the lakeFS commit succeeds. The database
row and descriptor use the same finalization timestamp.

## Descriptor schema, version 1

The JSON has `kind: "episerve.benchmark-definition"` and `schema_version: 1`.

| Section | Information |
|---|---|
| `benchmark` | Original ID, name, description, target dataset and column roles, filters, covariates with aggregation rules, resolved history/test windows, input filename, source commit and component, forecast horizon, reference-point count, and timestamps |
| `generated_files` | Object QID, repository, branch, input/reference components, SHA-256 checksums, column lists, row counts, available date bounds, and source components |
| `recipe` | Frozen source pins, filters, windows, generation rules, merge details, and reproduction code |
| `generator` | Materializer format version and Python, pandas, and PyArrow versions |
| `exported_at` | UTC timestamp at which the descriptor snapshot was produced |

For a newly finalized object, `generated_files.commit` is absent: it means the
**exact containing commit of the retrieved descriptor**. A document cannot include
the hash of the commit containing itself. Never substitute the latest branch head
when resolving this implicit version.

For an older benchmark exported later, `generated_files.commit` explicitly names
its original data version. The descriptor's containing commit can differ.

Ownership, users, permissions, leaderboard registrations/scores, and the generated
chart images are outside this scientific-definition descriptor. A restored row
has `created_by = NULL`, so an administrator must explicitly assign ownership.
The chart can be regenerated from the preserved files. Later description edits or
retirements are not an event log in this finalization snapshot.

## Recovering when only lakeFS survives

1. Enumerate generated FDO objects and their historical versions in the
   `benchmarks` repository. Find their `benchmark-definition.json` components.
   Keep each component's **exact containing commit**.
2. Verify descriptor bytes against the checksum in that FDO version. Verify the
   input/reference bytes against the checksums in `generated_files`, using the
   explicit data commit or the descriptor's containing commit when implicit.
3. Validate and reconstruct the row without querying MariaDB, CKAN, or source
   datasets:

   ```python
   from pathlib import Path
   from app.benchmarks.descriptor import rebuild_row

   row = rebuild_row(
       Path("benchmark-definition.json").read_bytes(),
       object_commit="<exact lakeFS commit of this descriptor>",
   )
   ```

   This returns database column values, including the original benchmark ID and
   the reconstructed `materialized` JSON. Input/reference QIDs, checksums, and data
   commits are preserved. `target_preview` is initially NULL. Unsupported descriptor
   versions or invalid required metadata raise `DefinitionError`.
4. Recreate the database schema using administrator-managed schema backups or
   migrations, then insert these reviewed rows with their **original IDs**. Do not
   overwrite conflicting IDs or automatically import private user accounts. When
   multiple historical descriptors exist for one ID, compare their pinned
   definitions and resolve the desired version explicitly.
5. Regenerate images with `actions.store_target_preview(id)`. Restore registrations,
   scores, user accounts, and ownership separately; those are not reconstructed by
   this helper.

The row reconstruction helper performs no writes and does not regenerate the
Parquet files. There is no automatic whole-service database restore command.

## Exporting benchmarks finalized before descriptors existed

Run once using the application's configured database and lakeFS credentials:

```python
from app.benchmarks import actions

for row in actions.list_final():
    actions.export_definition(row["id"])
```

The exporter checks the frozen FDO against the database file metadata, writes
only the JSON component and an updated FDO record in a new metadata commit, and
adds its pointer to `materialized.definition`. It preserves the original data QID,
input/reference versions, checksums, Parquet bytes, and leaderboard input URLs.
Repeated exports reuse an existing pointer. The snapshot includes the current
description and available frozen scientific definition; it cannot recover earlier
edits that were never preserved. Older storage layouts must first be migrated to
the generated-object layout.
