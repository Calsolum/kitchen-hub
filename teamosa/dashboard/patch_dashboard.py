"""Add the Tea tile, panel and Brew-now confirm to the Kitchen Hub dashboard.

  python3 patch_dashboard.py path/to/index.html     # edits in place; refuses if already patched

Every anchor must occur exactly once, so a changed dashboard fails loudly instead of
being patched in the wrong place.
"""
import sys
from pathlib import Path

HERE = Path(__file__).parent


def piece(name):
    return (HERE / name).read_text(encoding="utf-8")


EDITS = [
    # (anchor, text to insert, insert "after" or "before" the anchor)
    (".power-btn:disabled { opacity: 0.5; cursor: default; }\n", "tea.css", "after"),
    ("<div class=\"action-sub\">Countdown &amp; stopwatch</div>\n        </div>\n", "tea-tile.html", "after"),
    ("\n<div id=\"osk\">", "tea-modal.html", "before"),
    ("  else if (type==='guestrequests') loadGuestRequestsFocus();\n", "tea-openfocus.js", "after"),
    ("\ncheckPowerAllowed();\n", "tea.js", "after"),
    ("  closePowerModal();   // never leave a restart/shut-down question waiting behind the sleep screen\n",
     "tea-sleep.js", "after"),
]


def main(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if "tea-action-btn" in text:
        sys.exit(f"{path} already has the Tea tile; not patching twice")
    for anchor, name, where in EDITS:
        count = text.count(anchor)
        if count != 1:
            sys.exit(f"anchor for {name} found {count} times (need exactly 1): {anchor!r}")
        insert = piece(name)
        replacement = anchor + insert if where == "after" else insert + anchor
        text = text.replace(anchor, replacement)
    with open(path, "w", encoding="utf-8", newline="") as f:   # in place: same inode
        f.write(text)
    print(f"patched {path}: {len(EDITS)} insertions")


if __name__ == "__main__":
    main(sys.argv[1])
