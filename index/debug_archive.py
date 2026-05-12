"""
debug_archive.py
================
Prints the raw HTML returned by the archive for a given date,
then shows what the regex extracts from it.

Usage:
    python debug_archive.py 2014-08-03
"""
import re
import sys
import requests

date_str = sys.argv[1] if len(sys.argv) > 1 else "2014-08-03"
url = f"https://archive.opensensemap.org/{date_str}/"

print(f"Fetching: {url}\n")
r = requests.get(url, timeout=20, headers={"User-Agent": "osem-debug/1.0"})
print(f"Status : {r.status_code}")
print(f"Final URL (after redirects): {r.url}")
print(f"Content-Type: {r.headers.get('Content-Type', '?')}")
print(f"\n{'='*60}")
print("RAW RESPONSE (first 3000 chars):")
print('='*60)
print(r.text[:3000])
print('='*60)

# Test both regex patterns
print("\nPattern test (absolute URLs):")
pat1 = re.compile(r'href="(?:https?://[^"/]*)?' + r'/' + re.escape(date_str) + r'/([^/"]+)/"')
matches1 = pat1.findall(r.text)
print(f"  Matches: {matches1[:5]}")

print("\nPattern test (any href with date):")
pat2 = re.compile(re.escape(date_str) + r'/([^/"<\s]+)/')
matches2 = pat2.findall(r.text)
print(f"  Matches: {matches2[:5]}")

print("\nAll hrefs in page:")
all_hrefs = re.findall(r'href="([^"]*)"', r.text)
for h in all_hrefs[:20]:
    print(f"  {h!r}")
