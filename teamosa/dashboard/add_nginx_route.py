"""Insert the /teamosa/ location after /power/ in kitchen-locations.inc, in place.

The file is bind-mounted into the nginx container as a single file, so it must keep its
inode: rewrite the same file (r+ / truncate), never replace it. Prints no other content.
"""
import os
import sys

path = sys.argv[1]
POWER = """    location /power/ {
        proxy_pass http://host.docker.internal:9700/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
"""
TEAMOSA = """
    # Teamosa tea brewer (kitchen-teamosa: ~/teamosa/teamosa/server.py). Kiosk and tailnet only:
    # like /power/, it trusts X-Real-IP only from nginx, and nginx overwrites whatever a client sent.
    location /teamosa/ {
        proxy_pass http://host.docker.internal:9800/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
"""

inode_before = os.stat(path).st_ino
with open(path, "r+", encoding="utf-8", newline="") as f:
    text = f.read()
    if "location /teamosa/" in text:
        sys.exit("already has /teamosa/; nothing changed")
    if text.count(POWER) != 1:
        sys.exit(f"/power/ block found {text.count(POWER)} times (need 1); nothing changed")
    text = text.replace(POWER, POWER + TEAMOSA)
    f.seek(0)
    f.write(text)
    f.truncate()
inode_after = os.stat(path).st_ino
print(f"inserted /teamosa/; inode {inode_before} -> {inode_after} ({'same' if inode_before == inode_after else 'CHANGED'})")
