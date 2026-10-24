"""Uso: python tools/set_version.py 2.1.0  — atualiza core/version.py e version_info.txt."""
import pathlib
import re
import sys

v = sys.argv[1].lstrip("vV")
nums = [int(x) for x in (v.split(".") + ["0", "0", "0"])[:3]]
root = pathlib.Path(__file__).resolve().parent.parent
p = root / "core" / "version.py"
p.write_text(re.sub(r'VERSION = "[^"]*"', f'VERSION = "{v}"', p.read_text(encoding="utf-8")), encoding="utf-8")
vi = root / "version_info.txt"
s = vi.read_text(encoding="utf-8")
tup = f"({nums[0]}, {nums[1]}, {nums[2]}, 0)"
s = re.sub(r"filevers=\([^)]*\)", f"filevers={tup}", s)
s = re.sub(r"prodvers=\([^)]*\)", f"prodvers={tup}", s)
s = re.sub(r"'(FileVersion|ProductVersion)', '[^']*'", lambda m: f"'{m.group(1)}', '{v}'", s)
vi.write_text(s, encoding="utf-8")
print("versao", v)
