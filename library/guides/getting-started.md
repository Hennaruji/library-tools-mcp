This guide is the first stop for new contributors. It covers local setup and
the shape of the repository; deeper topics live in the backend docs.

# Getting started

## Prerequisites

You need Python 3.12+, Docker, and access to the shared Redis instance.
Ask in #platform for credentials.

## Local setup

Clone the repo and bring up the dev stack:

```bash
git clone git@example.com:acme/platform.git
cd platform
make dev-up
```

The stack starts the API, a worker, Redis and Postgres. Once it is up, the
service map looks like this:

![[image:img-service-map]]

## Repository layout

| path | contents |
|---|---|
| `api/` | HTTP handlers |
| `worker/` | background jobs |
| `storage/` | cache and database access |

# Next steps

Read the authentication guide before touching any handler, then the storage
docs if you work on persistence.
