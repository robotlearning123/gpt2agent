import os

# Keep the suite offline regardless of any ENABLED sentinel-bridge
# marker on the host machine (see gpt2agent/sentinel_bridge.py).
os.environ.setdefault("GPT2AGENT_SENTINEL_BRIDGE_OFF", "1")
