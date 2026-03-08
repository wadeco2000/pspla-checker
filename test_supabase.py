import requests
from dotenv import load_dotenv
import os

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

url = f"{SUPABASE_URL}/rest/v1/Companies"
headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal"
}

test_record = {
    "company_name": "TEST COMPANY",
    "website": "https://test.com",
    "phone": None,
    "email": None,
    "address": None,
    "region": "Test",
    "pspla_licensed": None,
    "pspla_name": None,
    "pspla_address": None,
    "pspla_license_number": None,
    "pspla_license_status": None,
    "pspla_license_expiry": None,
    "license_type": None,
    "match_method": None,
    "companies_office_name": None,
    "companies_office_address": None,
    "source_url": None,
    "last_checked": None,
    "notes": None
}

response = requests.post(url, headers=headers, json=test_record)
print(f"Status: {response.status_code}")
print(f"Response: {response.text}")
