# Authentication

All requests pass through the gateway, which validates a short-lived access
token and attaches the user to the request context.

## Login flow

1. The client posts credentials to `/login`.
2. The gateway issues an access token (15 minutes) and a refresh token (30 days).
3. The client sends the access token on every request.

![[image:img-login-sequence]]

## Refresh tokens

Refresh tokens are opaque, stored hashed, and bound to a device id.

### Rotation

Every refresh issues a new refresh token and revokes the old one. Reusing a
revoked token revokes the whole family, which logs the device out.

```python
def refresh(old_token: str, device_id: str) -> TokenPair:
    record = tokens.lookup(hash_token(old_token))
    if record is None:
        raise InvalidToken()
    if record.revoked:
        tokens.revoke_family(record.family_id)
        raise TokenReuse(record.family_id)
    if record.device_id != device_id:
        raise DeviceMismatch()
    tokens.revoke(record.id)
    return issue_pair(record.user_id, device_id, family_id=record.family_id)
```

![[image:img-token-rotation]]

### Expiry

Refresh tokens expire after 30 days of inactivity, not 30 days from issue.

## SSO quirks

- Okta sends group claims only when the `groups` scope is requested.
- Azure AD may return the email in `upn` instead of `email`.

```python
email = claims.get("email") or claims.get("upn")
```
