# Caching

Reads go through a Redis cache in front of Postgres. The cache is a
convenience: every path must still work if Redis is down.

## Key conventions

Keys are namespaced by entity and version:

```
user:{id}:profile:v3
org:{id}:members:v1
```

Bump the version suffix whenever the cached shape changes.

## Invalidation

Writes invalidate after the database commit succeeds, never before.

### Fan-out deletes

Changing an org invalidates every member's profile key. Use the batched
helper so a large org does not issue thousands of round trips:

```python
def invalidate_org(org_id: int) -> None:
    member_ids = db.org_member_ids(org_id)
    cache.delete_many(f"user:{uid}:profile:v3" for uid in member_ids)
    cache.delete(f"org:{org_id}:members:v1")
```

![[image:img-fanout]]

## TTL rules

Default TTL is 10 minutes. Anything permission-related gets 60 seconds.
Never cache without a TTL.
