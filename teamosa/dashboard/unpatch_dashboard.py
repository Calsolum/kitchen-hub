"""Rebuild a pre-Tea dashboard by removing the exact pieces patch_dashboard.py inserted.

  python3 unpatch_dashboard.py patched.html out.html   # uses the pieces next to this script

Each removal must match exactly once; writes a new file and never touches the input.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from patch_dashboard import EDITS, piece  # noqa: E402

src, out = Path(sys.argv[1]), Path(sys.argv[2])
text = src.read_text(encoding="utf-8")
for anchor, name, where in EDITS:
    joined = anchor + piece(name) if where == "after" else piece(name) + anchor
    if text.count(joined) != 1:
        sys.exit(f"{name}: inserted text found {text.count(joined)} times (need 1); nothing written")
    text = text.replace(joined, anchor)
if "tea-action-btn" in text or "teaCloseModal" in text:
    sys.exit("Tea code still present after removal; nothing written")
if out.exists():
    sys.exit(f"{out} exists; not overwriting")
with open(out, "w", encoding="utf-8", newline="") as f:
    f.write(text)
print(f"wrote {out}: {len(text.encode('utf-8'))} bytes")
