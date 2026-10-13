# TR-223 evidence run — credential half acceptance (2026-10-13 09:18 UTC)

## Live credential verification (2026-10-13, in-tick)
- `GET https://openrouter.ai/api/v1/models` -> HTTP 200 (public catalog)
- `GET https://openrouter.ai/api/v1/key` with the provisioned OPENROUTER_API_KEY -> HTTP **401**
- registry openrouter active lanes: 274, all unranked — a credential fact, not a ranking failure
- audit unranked total (this tick, measured): 306 of 772 active/priced lanes

## Command + exit code
```
$ python3 scripts/router_rank_audit.py --json
exit: 0 
```
## Key output fields
```json
{
 "active": 772,
 "unranked": 306,
 "thin": 79,
 "credential_blocked_unranked": {
  "openrouter": 274
 },
 "blocked_by_unreachable": 2
}
```
## Text report carries the named reason
```
   CREDENTIAL-BLOCKED (cannot be measured until the key is rotated): {'openrouter': 274} — reason: credential-expired
```
## next-wave targets (credential-blocked providers excluded)
```
# 54 lanes across 9 providers
#   clinepass        15
#   commandcode      3
#   commandcode-2    3
#   fireworks-ai     12
#   grok-build       1
#   neuralwatt       2
#   synthetic        1
#   xkiro            9
#   xkiro-2          8
```
## Tests (this change)
```
4 passed in 0.27s
```
