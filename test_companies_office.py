import sys
sys.path.insert(0, '.')
from searcher import check_companies_office, check_pspla

# Simulate full flow for Hines Security
company_name = "Hines Security"
print(f"=== Testing: {company_name} ===")

# Step 1: PSPLA check
pspla = check_pspla(company_name)
print(f"PSPLA result: {pspla}")

# Step 2: Companies Office using PSPLA matched name + address
if pspla.get("licensed"):
    co = check_companies_office(pspla["matched_name"], pspla_address=pspla.get("pspla_address"))
    print(f"Companies Office result: {co}")
else:
    co = check_companies_office(company_name)
    print(f"Companies Office result: {co}")
