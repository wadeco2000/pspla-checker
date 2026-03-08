import requests
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}


def get_unreviewed():
    url = f"{SUPABASE_URL}/rest/v1/Companies?pspla_licensed=eq.false&manually_reviewed=is.null&select=*&order=company_name.asc"
    r = requests.get(url, headers=HEADERS)
    data = r.json()
    if isinstance(data, list):
        return data
    return []


def update_company(company_id, status, notes):
    url = f"{SUPABASE_URL}/rest/v1/Companies?id=eq.{company_id}"
    payload = {
        "manual_status": status,
        "manual_notes": notes,
        "manually_reviewed": True
    }
    r = requests.patch(url, headers=HEADERS, json=payload)
    return r.status_code in [200, 204]


def main():
    print("=" * 60)
    print("  PSPLA Manual Review Tool")
    print("=" * 60)
    print("Review companies marked as NOT LICENSED.")
    print("Commands: l=licensed, u=unlicensed, s=skip, q=quit\n")

    companies = get_unreviewed()

    if not companies:
        print("No unreviewed companies found.")
        return

    print(f"Found {len(companies)} companies to review.\n")

    for i, c in enumerate(companies):
        print(f"\n--- Company {i+1} of {len(companies)} ---")
        print(f"Name:          {c.get('company_name', '-')}")
        print(f"Website:       {c.get('website', '-')}")
        print(f"Region:        {c.get('region', '-')}")
        print(f"Phone:         {c.get('phone', '-')}")
        print(f"Address:       {c.get('address', '-')}")
        print(f"PSPLA checked: {c.get('pspla_name') or 'no match found'}")
        print(f"Match method:  {c.get('match_method', '-')}")
        print()
        print("What did you find when you checked manually?")
        print("  l = They ARE licensed (we missed it)")
        print("  u = Confirmed NOT licensed")
        print("  s = Skip for now")
        print("  q = Quit")
        print()

        while True:
            action = input("Your verdict [l/u/s/q]: ").strip().lower()

            if action == "q":
                print("\nExiting review. Progress saved.")
                return

            if action == "s":
                print("Skipped.")
                break

            if action in ["l", "u"]:
                notes = input("Notes (what you searched, what you found): ").strip()
                status = "licensed" if action == "l" else "unlicensed"

                if update_company(c["id"], status, notes):
                    if action == "l":
                        print(f"  Saved: LICENSED - we need to improve the checker for this one.")
                    else:
                        print(f"  Saved: Confirmed NOT LICENSED.")
                else:
                    print("  Error saving — try again.")
                    continue
                break

            print("  Please enter l, u, s, or q")

    print("\n" + "=" * 60)
    print("  Review complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
