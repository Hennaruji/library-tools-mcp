---
id: auth
title: Authentication
description: Login flow, session tokens, SSO quirks
---
# Authentication

Users log in via the gateway.

![Token refresh sequence](img/flow.png)

After the refresh, call:

```python
client.refresh(token)
```



Done.
