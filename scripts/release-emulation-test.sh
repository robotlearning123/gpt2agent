#!/usr/bin/env bash
# v0.0.14 large human-user emulation test (outsider gate).
# Exercises the BUILT artifact the way a real user would: fresh venv install,
# isolated HOME, first-run flows, live MCP stdio client calls, upgrade path.
# Never touches the real pipx env, real ~/.claude, or real ~/.codex (token is
# read-only via CODEX_HOME passthrough only where noted).
set -uo pipefail
W="${1:?usage: release-emulation-test.sh <release-worktree>}"
OUT=$W/artifacts/verify/human-emulation-20260915
mkdir -p "$OUT"
PASS=0; FAIL=0
check() { # check <name> <exit-code> [detail]
  if [ "$2" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS  $1 ${3:-}"; else FAIL=$((FAIL+1)); echo "FAIL  $1 ${3:-}"; fi
}

echo "═══ A. build & artifact checks ═══"
cd -P "$W" || exit 2
EXPECTED=$(python3 -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
rm -rf dist && python -m build > "$OUT/build.log" 2>&1
check "A1 python -m build" $? "$(find dist -maxdepth 1 -type f -printf '%f ' 2>/dev/null)"
python -m twine check dist/* > "$OUT/twine.log" 2>&1
check "A2 twine check" $?
WHEEL=$(find dist -maxdepth 1 -name '*.whl' | head -1)

echo "═══ B. outsider first install (isolated HOME, no dev deps) ═══"
VENV=/tmp/gpt2agent-rel14-venv; ISOHOME=/tmp/gpt2agent-rel14-home
rm -rf "$VENV" "$ISOHOME"; mkdir -p "$ISOHOME"
python -m venv "$VENV" > /dev/null 2>&1
"$VENV/bin/pip" install --quiet "$WHEEL" > "$OUT/pip-install.log" 2>&1
check "B1 pip install wheel (clean venv)" $?
V=$("$VENV/bin/gpt2agent" --version 2>&1)
if [ "$V" = "gpt2agent $EXPECTED" ] || [ "$V" = "$EXPECTED" ]; then VB=0; else VB=1; fi
check "B2 gpt2agent --version == pyproject version" "$VB" "got: $V (expected $EXPECTED)"

echo "═══ C. first-run flows without token (isolated HOME) ═══"
HOME="$ISOHOME" CODEX_HOME="$ISOHOME/.codex" "$VENV/bin/gpt2agent" doctor > "$OUT/doctor-notoken.log" 2>&1
RC=$?
if [ "$RC" -eq 2 ]; then C1OK=0; else C1OK=1; fi
check "C1 doctor no-token exits 2 with clean message" "$C1OK" "exit=$RC $(tail -1 "$OUT/doctor-notoken.log" | head -c 120)"
HOME="$ISOHOME" CODEX_HOME="$ISOHOME/.codex" "$VENV/bin/gpt2agent" install --client claude-code > "$OUT/install-claude.log" 2>&1
check "C2 install --client claude-code (isolated HOME)" $? "$(grep -c gpt2agent "$ISOHOME/.claude.json" 2>/dev/null || echo 0) refs in .claude.json"

echo "═══ D. live doctor with real token (read-only) ═══"
# Owner hosts may have the opt-in sentinel bridge (ENABLED marker). Its Python
# deps are not part of the gpt2agent wheel — install them into the emulation
# venv so the mint path is exercised like on the real owner env.
if [ -f "$HOME/.gpt2agent/sentinel-bridge/ENABLED" ]; then
  "$VENV/bin/pip" install --quiet colorama esprima > /dev/null 2>&1 || true
fi
# Documented contract (doctor.py): exit 0 only when nothing failed AND nothing
# blocked; under the upstream blockade exit 1 is EXPECTED with "0 failed".
"$VENV/bin/gpt2agent" doctor > "$OUT/doctor-live.log" 2>&1
RC=$?
SUM=$(tail -1 "$OUT/doctor-live.log")
if [ $RC -eq 1 ] && echo "$SUM" | grep -q "0 failed"; then RC=0; fi
check "D1 doctor live: exit 1 + '0 failed' under blockade" $RC "exit=$RC $SUM"

echo "═══ E. MCP stdio client emulation (installed artifact, real client lib) ═══"
cat > /tmp/gpt2agent-rel14-mcp.py <<'PYEOF'
import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    results = {}
    params = StdioServerParameters(command="/tmp/gpt2agent-rel14-venv/bin/gpt2agent", args=["run", "--stdio"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            results["tool_count"] = len(tools)
            conv_tools = ["chat","agent","deep_research","deep_research_heavy","gpt_chat",
                          "memory_create_via_chat","generate_image","code_interpreter","canvas_execute"]
            schema_ok = {}
            for t in tools:
                if t.name in conv_tools:
                    props = (t.inputSchema or {}).get("properties", {})
                    req = (t.inputSchema or {}).get("required", [])
                    schema_ok[t.name] = "manual" in props and "manual" not in req
            results["manual_param_on_9"] = all(schema_ok.values()) and len(schema_ok) == 9
            r = await s.call_tool("account_status", {})
            sub = json.loads(r.content[0].text).get("subscription") if r.content and not r.isError else None
            results["account_status"] = "OK" if sub else "BAD"
            r = await s.call_tool("chat", {"prompt": "emulation probe", "manual": True})
            h = json.loads(r.content[0].text) if r.content and not r.isError else {}
            results["chat_manual"] = "OK" if h.get("status") == "manual_handoff" and h.get("prompt") == "emulation probe" else "BAD"
            r = await s.call_tool("list_conversations", {"limit": 1})
            results["list_conversations"] = "OK" if (r.content and not r.isError) else "BAD"
    print(json.dumps(results))

asyncio.run(main())
PYEOF
"$VENV/bin/python" /tmp/gpt2agent-rel14-mcp.py > "$OUT/mcp-client.json" 2> "$OUT/mcp-client.err"
check "E1 MCP stdio client session" $? "$(head -c 300 "$OUT/mcp-client.json" 2>/dev/null)"
python3 -c "
import json;d=json.load(open('$OUT/mcp-client.json'))
assert d['tool_count']>=25, d; assert d['manual_param_on_9'], d
assert d['account_status']=='OK' and d['chat_manual']=='OK' and d['list_conversations']=='OK', d
" 2>/dev/null
check "E2 tool_count>=25 + manual on 9 + live calls" $?

echo "═══ F. upgrade path 0.0.13 -> 0.0.14 (scratch venv) ═══"
UVENV=/tmp/gpt2agent-rel14-upg
rm -rf "$UVENV"; python -m venv "$UVENV" > /dev/null 2>&1
"$UVENV/bin/pip" install --quiet "gpt2agent==0.0.13" > "$OUT/pip-013.log" 2>&1
RC1=$?
"$UVENV/bin/pip" install --quiet --upgrade "$WHEEL" > "$OUT/pip-upg.log" 2>&1
RC2=$?
V2=$("$UVENV/bin/gpt2agent" --version 2>&1)
check "F1 0.0.13 install + upgrade to wheel" $((RC1|RC2)) "now: $V2"

echo "═══ G. uninstall cleanliness ═══"
"$UVENV/bin/pip" uninstall -y -q gpt2agent > /dev/null 2>&1
if [ ! -e "$UVENV/bin/gpt2agent" ]; then G1OK=0; else G1OK=1; fi
check "G1 pip uninstall" "$G1OK"

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
