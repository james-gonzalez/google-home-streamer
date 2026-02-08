import os
import sys

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Disable auto-discovery during tests
os.environ.setdefault("AUTO_START_DISCOVERY", "0")
