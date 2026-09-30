import json, re, sys
from pathlib import Path

MANIFEST = Path("custom_components/downtime_auditor/manifest.json")
RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:b(\d+))?$")

def bump(cur, part, pre):
   m = RE.match(cur) or sys.exit(f"Bad version {cur!r}")
   ma, mi, pa = map(int, m.group(1, 2, 3))
   if m.group(4) is not None:  # currently a beta
       base = f"{ma}.{mi}.{pa}"
       return f"{base}b{int(m.group(4)) + 1}" if pre else base
   if part == "major": ma, mi, pa = ma + 1, 0, 0
   elif part == "minor": mi, pa = mi + 1, 0
   else: pa += 1
   return f"{ma}.{mi}.{pa}" + ("b1" if pre else "")

m = json.loads(MANIFEST.read_text())
m["version"] = bump(m["version"], sys.argv[1], sys.argv[2] == "true")
MANIFEST.write_text(json.dumps(m, indent=2) + "\n")
print(m["version"])