import sys
from pathlib import Path

# The Functions app lives in api/, and its modules import each other as
# `shared.*`. Put api/ on the path so tests can import them the same way.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
