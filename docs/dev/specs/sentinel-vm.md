# Spec: Sentinel VM — clean-room Turnstile token generator

Date: 2026-09-17 · Goal mode: "until fix, then test, verify, release" ·
Provenance: technique studied from public reverse-engineering write-ups of
the chatgpt.com sentinel gate (2026); NO third-party code is copied — this
module is a from-scratch implementation against captured fixtures. Verified
live 2026-09-17: this technique produces accepted tokens on
/backend-api/sentinel/chat-requirements + /f/conversation (200 SSE).

## Goal

`gpt2agent/sentinel_vm.py`: given the `turnstile.dx` bytecode from a
chat-requirements response, produce the `openai-sentinel-turnstile-token`
value — restoring the REST conversation path (chat/DR/images/canvas/...)
without a browser. Deterministic under a supplied seed.

## Public interface

```python
def turnstile_token(dx: str, p_token: str, ip_latlng: str,
                     rng: random.Random) -> str
```

- `dx`: base64 bytecode string from requirements["turnstile"]["dx"]
- `p_token`: the SAME `p` string sent in the requirements request body
  (it is the XOR key for layer 1)
- `ip_latlng`: e.g. `"39.04,-77.49"` (account geo)
- `rng`: seeded Random supplying every random the payload needs

Raise `SentinelVMError(RuntimeError)` naming the failing stage on any
structural surprise (never return garbage).

## Pipeline (five stages)

### S1 — layer-1 decode
`base64.b64decode(dx)` → utf-8 string → XOR with `p_token` (cyclic
keystream: `out[i] = chr(ord(s[i]) ^ ord(key[i % len(key)]))`) → the result
is a JSON **list of instructions** (each ~88-95 rows per live program).

### S2 — instruction schema
Each row: a list whose FIRST element selects the operation by number;
remaining elements are operands. Operands that are small integers (<64)
are often VARIABLE-REFERENCE indices (see mapping below); other operands
are literal values (numbers, strings, [], null). Fixture
`instructions_sample` rows in tests/fixtures/sentinel_vm/fixtures.json show
real shapes.

### S3 — interpretation (an interpreter, NOT a decompiler)
Track a variable table and an opcode-name table (index → name). Known
opcodes (number → semantics):
- 2 SET_VALUE(var, value): assign literal (numbers; "[]" → empty array;
  None → null; strings verbatim)
- 1 XOR_STR(a, b): `vars[a] = xor(vars[a], vars[b])` (same cyclic XOR)
- 3/4/19 BTOA family: `vars[a] = btoa(str(vars[a]))`
- 18 ATOB: decode
- 5 ADD_OR_PUSH(a, b): array-append if array else string/number add
- 6 ARRAY_ACCESS(dst, arr, idx); 24 BIND_METHOD(dst, obj, name)
- 7 CALL / 13 TRY_CALL / 17 CALL_AND_SET / 22 TEMP_STACK_CALL:
  call-with-assignment, try/catch wrapper variants
- 10 window: resolve window-ish root; 11 GET_SCRIPT_SRC; 12 GET_MAP
- 14/15 JSON parse/stringify
- 20 IF_EQUAL_CALL / 21 IF_DIFF_CALL / 23 IF_DEFINED_CALL: conditional ops
  (IF_DIFF uses abs(a-b) > c)
- 27 REMOVE_OR_SUBTRACT; 29 LESS_THAN; 31 INCREMENT; 33 MULTIPLY; 8 COPY
  (alias one var name to another); 34 MOVE (unused)
Unknown opcodes: record and skip (live programs tolerate this).

What we must RECOVER from interpretation (everything else is bookkeeping):
1. **xor_key**: the string variable used as the second operand of the late
   XOR_STR runs (also appears as cyclic key in the final wrap)
2. **assignments**: an ordered mapping payload-key → literal-ish value
   where value is one of the RECOGNIZED SHAPES:
   - a float/number (as string)
   - `singlebtoa(<inner>)`
   - `doublexor(<number>)`
   - the markers `ipinfo`, `element`, `location`, `random_1`, `random_2`,
     `vendor`, `localstorage`, `history`
   Implementations may recover these by interpreting the calls that build
   them (GET_MAP/CALL results) — the CONTRACT is: for each payload key the
   interpreter must classify it into exactly one shape.

### S4 — payload build (exact recipes)
For each (key, value):
- number: `b64(xor(str(value), xor_key))`
- `singlebtoa(X)`: `b64(X)` (no xor)
- `doublexor(N)`: `v1=b64(xor(N,N)); v2=b64(xor(v1,v1)); out=b64(v2)`
- `ipinfo`: `b64(xor(ip_latlng, xor_key))`
- `element`: `b64(xor(ELEMENT_JSON, xor_key))` with ELEMENT_JSON =
  `{"x":0,"y":1219,"width":37.8125,"height":30,"top":1219,"right":37.8125,"bottom":1249,"left":0}`
  (compact separators)
- `location`: `b64(xor("https://chatgpt.com/", xor_key))`
- `random_1`: `r=rng.random(); b64(xor(str(r), str(r)))` (self-xor!)
- `random_2`: `rng.random()` BARE (no xor, no b64)
- `vendor`: `b64(xor('["Google Inc.","Win32",8,0]', xor_key))`
- `localstorage`: `b64(xor(LS_KEYS, xor_key))` with LS_KEYS the exact
  comma-joined key list (see tests — copied verbatim there)
- `history`: `b64(xor(str(rng.randint(1,5)), xor_key))`
All b64 = standard alphabet, utf-8, no newlines. xor = the same cyclic XOR.

### S5 — final wrap
`json.dumps(payload, separators=(",", ":"))` → xor with xor_key → b64 →
return. Payload dict ORDER: insertion order recovered in S3 (fixtures pin
this).

## Acceptance oracle (binding)

1. `pytest -q tests/` green; existing tests untouched.
2. tests/test_sentinel_vm.py loads
   tests/fixtures/sentinel_vm/fixtures.json: THREE fixtures, each
   `(dx, p, ip, seed=1000+i, expected_token)` —
   `turnstile_token(...) == expected_token` **byte-identical**.
3. Structural round-trip: S1 decode of a fixture dx yields a JSON list of
   ≥80 rows (assert in test, no network).
4. Unknown-opcode and bad-base64 paths raise SentinelVMError (fail-closed).
5. No new runtime deps (stdlib only). No network in module.
6. `python -c "import gpt2agent.server"` unaffected.

## Non-goals (this PR)

- SentinelGate integration (p-fingerprint + conduit flow) = next PR.
- PoW changes (ours already passes).
- Any third-party code vendoring (clean-room; technique attribution in
  NOTICES.md prose only).

## Deliverables

`gpt2agent/sentinel_vm.py`, `tests/test_sentinel_vm.py` (fixture-driven,
seeded, offline), this spec.
