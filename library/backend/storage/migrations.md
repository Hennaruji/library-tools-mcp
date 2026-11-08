# Migrations

Schema changes ship as numbered migration files and must be backwards
compatible with the previous release.

## Adding a column

Add it nullable, deploy, backfill, then add the constraint in a later release.

### Examples

```sql
ALTER TABLE users ADD COLUMN locale TEXT;
```

## Dropping a column

Stop reading it, deploy, stop writing it, deploy, then drop it.

### Examples

```sql
ALTER TABLE users DROP COLUMN legacy_flags;
```

This doc has no diagram yet: ![[image:img-missing]]
