import sys
sys.path.insert(0, '.')
from searcher import scrape_website, extract_company_info, check_pspla

url = "https://www.abse.co.nz/"
print("Scraping website...")
page_text = scrape_website(url)

print("Extracting company info...")
info = extract_company_info(url, page_text, "ABSE security electrical")
print(f"Extracted info: {info}")

if info:
    names_to_try = []
    if info.get("company_name"): names_to_try.append(info["company_name"])
    if info.get("legal_name") and info["legal_name"] not in names_to_try: names_to_try.append(info["legal_name"])
    for other in (info.get("other_names") or []):
        if other and other not in names_to_try: names_to_try.append(other)

    print(f"\nNames to try: {names_to_try}")
    for name in names_to_try:
        result = check_pspla(name, website_region=info.get("region"))
        print(f"PSPLA check for '{name}': licensed={result.get('licensed')}, matched={result.get('matched_name')}")
        if result.get("licensed"):
            print("FOUND!")
            break
