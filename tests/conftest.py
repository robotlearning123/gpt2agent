import os

# Keep the suite offline regardless of any ENABLED sentinel-bridge
# marker on the host machine (see gpt2agent/sentinel_bridge.py).
os.environ.setdefault("GPT2AGENT_SENTINEL_BRIDGE_OFF", "1")

# The shared rate limiter must not pace tests — its file-backed state and
# sleeps are exercised in tests/test_ratelimit.py with a tmp state file.
os.environ.setdefault("GPT2AGENT_RATELIMIT_OFF", "1")
