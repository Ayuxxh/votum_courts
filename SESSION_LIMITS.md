# eCourts DC — Session & Rate Limit Analysis

> Tested: 2026-04-11 via live HTTP probes against `services.ecourts.gov.in/ecourtindia_v6`

---

## TL;DR

| Limit | Value |
|---|---|
| Session cookie | `SERVICES_SESSID` (PHP session) |
| Session idle TTL | ~24 min (PHP default); active sessions last 25+ min |
| `app_token` | Always `""` — server never validates it for AJAX |
| Rate limit | ~20–30 rapid requests → IP blocked for **≥ 3 min** |
| Safe request rate | ≥ 1.5 s delay per thread, ≤ 4 concurrent workers |

---

## `app_token` — Effectively Unused

The homepage serves:
```html
<input type="hidden" name="app_token" id="app_token" value="">
```

`csrf-magic.js` is supposed to populate this from server responses. In practice, **no endpoint ever returns a new `app_token`** in its JSON response:

| Endpoint | Returns `app_token`? |
|---|---|
| `casestatus/fillDistrict` | No |
| `casestatus/getCaptcha` | No |
| `casestatus/set_data` | No |
| `casestatus/submitPartyName` | No |

The server accepts `app_token=""` on all AJAX calls. The **CAPTCHA** is the real anti-bot layer.

**Code impact:** `initialize_session()` always captures `""`. The rolling-token update block in `_post()` (`if 'app_token' in json_resp`) never fires. This is harmless — everything works correctly with an empty token.

---

## Session Cookie

| Cookie | Role | Format | When set |
|---|---|---|---|
| `SERVICES_SESSID` | PHP session | 26-char alphanumeric | 200 responses only |
| `JSESSION` | WAF/load-balancer counter | Integer | Every request, incl. 403 |

`SERVICES_SESSID` has no `Max-Age` or `Expires` — it's a browser-session cookie client-side. Server-side PHP default `session.gc_maxlifetime = 1440 s` (24 min inactivity).

**Empirical:** `build_dc_json.py` ran for 25+ min processing all 36 states with zero session-expiry errors. Only failures were `RemoteDisconnected` (TCP drops). Active sessions survive the full run.

**Idle timeout boundary:** < 24 min if idle; indefinite if requests keep flowing.

---

## Rate Limiting

The binding operational constraint — stricter than session TTL.

- **Trigger:** ~20–30 requests in rapid succession
- **Block duration:** ≥ 3 minutes (confirmed by testing)
- **Scope:** IP-level — affects Python, curl, and browser once triggered
- **Mechanism:** WAF rule tracked via `JSESSION` counter cookie + IP
- **Response:** `HTTP 403`, 0 bytes, empty body

`JSESSION` resets on every blocked response with a new integer value — it functions as a WAF visitor tracker, not a real session.

---

## Implications for `keyword_case_monitor`

### Current settings are correct

```python
DEFAULT_DELAY_SECONDS = 1.5   # safe — well below WAF burst threshold
DEFAULT_WORKERS = 4           # borderline — keep at ≤ 4
```

4 workers × 1.5 s delay = ~2.7 req/s combined. This stays under the WAF threshold in practice.

### Retry adapter handles TCP drops

```python
Retry(total=4, backoff_factor=1.0)  # waits 1s, 2s, 4s between retries
```

The exponential backoff naturally spaces out retries, avoiding cascading blocks from a single flaky endpoint.

### Known gap — silent session expiry mid-run

If a worker thread sits idle in the executor queue for >24 min before starting, its `SERVICES_SESSID` may expire. The server returns `status=0` with an error body. The current code logs it but **does not re-initialize the session**. To fix:

```python
# In _post(), after receiving status==0:
if json_resp.get('status') == 0 and 'session' in str(json_resp.get('msg', '')).lower():
    self.initialize_session()
    # retry once
```

This edge case is unlikely in normal runs (all workers start within seconds of each other) but relevant for very large batches with many queued workers.
