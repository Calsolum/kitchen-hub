"""Replace one Tea block (the tea.js or tea.css text) in the live dashboard with a new version.

  python3 swap_tea_js.py index.html old_tea.js new_tea.js
  python3 swap_tea_js.py index.html old_tea.css new_tea.css

Only the exact old block changes, so edits other sessions made elsewhere in the file are kept.
Refuses unless the old block occurs exactly once, and if the file changes while it works.
Writes a temp file in the same directory and renames it over the original (atomic: readers
see the old or the new file, never half of one), keeping the owner, group and mode. The
dashboard is served by path from ~/dashboard (not a bind mount), so a new inode is fine.
"""
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
old = Path(sys.argv[2]).read_text(encoding="utf-8")
new = Path(sys.argv[3]).read_text(encoding="utf-8")
before = path.read_bytes()
text = before.decode("utf-8")
if text.count(old) != 1:
    sys.exit(f"old Tea block found {text.count(old)} times (need 1); nothing written")
out = text.replace(old, new).encode("utf-8")
st = os.stat(path)
tmp = path.with_name(path.name + ".teamosa-tmp")
with open(tmp, "wb") as f:
    f.write(out)
    f.flush()
    os.fsync(f.fileno())
os.chown(tmp, st.st_uid, st.st_gid)
os.chmod(tmp, st.st_mode & 0o7777)
if path.read_bytes() != before:
    tmp.unlink()
    sys.exit("file changed while patching; nothing written")
os.replace(tmp, path)
print(f"swapped {sys.argv[3]}: {len(before)} -> {len(out)} bytes")
