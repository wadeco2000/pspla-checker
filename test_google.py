import requests
from dotenv import load_dotenv
import os

load_dotenv()

r = requests.get(
    'https://serpapi.com/search',
    params={
        'api_key': os.getenv('SERPAPI_KEY'),
        'engine': 'google',
        'q': 'security camera installer Auckland New Zealand',
        'num': 5,
        'gl': 'nz'
    }
)

print("Status:", r.status_code)
data = r.json()
if "organic_results" in data:
    print(f"Found {len(data['organic_results'])} results:")
    for item in data["organic_results"]:
        print(" -", item.get("title"), "|", item.get("link"))
else:
    print("Response:", r.text[:500])
