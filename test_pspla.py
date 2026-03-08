import sys
sys.path.insert(0, '.')
from searcher import check_pspla

# Test with trading name that differs from registered name
tests = [
    "Hines Security",
    "Auckland CCTV",
    "Armourguard",
    "Geeks on Wheels",
]

for name in tests:
    result = check_pspla(name)
    print(f"{name}: {result}")
