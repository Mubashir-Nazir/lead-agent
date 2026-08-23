import csv
import json
import os
import re
import time
from urllib.parse import urljoin, urlparse
import pandas as pd
from openai import OpenAI
from playwright.sync_api import sync_playwright

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Gemini API Client using free OpenAI-compatible endpoint
client = (
    OpenAI(
        api_key=GEMINI_API_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    if GEMINI_API_KEY
    else None
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"
)
EMAIL_JUNK = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    "@sentry",
    "example.com",
    "@wixpress",
)
CONTACT_HINTS = ("contact", "about", "kontakt", "reach", "touch", "impressum")
SEARCH_BLOCKLIST = (
    "duckduckgo.com",
    "google.com",
    "youtube.com",
    "facebook.com",
    "instagram.com",
    "yelp.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "yellowpages.com",
    "wikipedia.org",
)


def html_to_text(html: str) -> str:
    """Strips HTML noise to minimize token size for AI analysis."""
    text = re.sub(
        r"<(script|style|svg|noscript)[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:12000]


def gemini_extract_contacts(text: str) -> dict:
    """Fallback AI extraction via Gemini if regex fails."""
    if not client or not text:
        return {"email": "N/A", "phone": "N/A", "socials": []}

    prompt = (
        "Extract official business contact details from the following website text. "
        "Return strictly a JSON object with keys: 'email' (string or 'N/A'), "
        "'phone' (string or 'N/A'), and 'socials' (list of social media URL strings).\n\n"
        f"TEXT:\n{text}"
    )

    try:
        response = client.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[
                {
                    "role": "system",
                    "content": "You are a contact extraction assistant. Output strictly valid JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        data = json.loads(response.choices[0].message.content or "{}")
        return {
            "email": data.get("email", "N/A"),
            "phone": data.get("phone", "N/A"),
            "socials": data.get("socials", []),
        }
    except Exception as e:
        print(f"    [!] Gemini AI Extraction Error: {e}")
        return {"email": "N/A", "phone": "N/A", "socials": []}


def search_fallback_website(page, company_name: str, city: str) -> str:
    """If no website on Google Maps, search Google/DuckDuckGo to find company website."""
    query = f"{company_name} {city}"
    print(f"    [*] No website on Maps. Searching web for: '{query}'...")
    try:
        page.goto(
            f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}",
            timeout=15000,
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(1000)

        for link in page.locator("a.result__url").all()[:10]:
            href = (link.get_attribute("href") or "").strip()
            if href.startswith("//"):
                href = "https:" + href
            if href.startswith("http") and not any(
                b in href.lower() for b in SEARCH_BLOCKLIST
            ):
                print(f"    [✓] Discovered Website via Web Search: {href}")
                return href
    except Exception as e:
        print(f"    [!] Search fallback error: {e}")
    return "N/A"


def deep_crawl_website(context, website_url: str, initial_phone: str) -> dict:
    """Navigates website, discovers contact subpages, and extracts contact info."""
    data = {
        "Email": "N/A",
        "Phone": initial_phone or "N/A",
        "Socials": [],
        "Website": website_url,
    }
    if not website_url or website_url == "N/A" or "google.com" in website_url:
        return data

    if not website_url.startswith("http"):
        website_url = "https://" + website_url

    page = None
    try:
        page = context.new_page()
        page.goto(website_url, timeout=20000, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)

        # Discover Contact/About subpages
        subpage_urls = [website_url]
        try:
            for a in page.locator("a[href]").all()[:150]:
                href = (a.get_attribute("href") or "").strip()
                if any(h in href.lower() for h in CONTACT_HINTS):
                    full_url = urljoin(website_url, href).split("#")[0]
                    if full_url not in subpage_urls and len(subpage_urls) < 3:
                        subpage_urls.append(full_url)
        except Exception:
            pass

        collected_emails = set()
        collected_phones = set()
        homepage_text = ""

        # Crawl homepage and discovered contact pages
        for idx, url in enumerate(subpage_urls):
            try:
                if idx > 0:
                    page.goto(
                        url, timeout=15000, wait_until="domcontentloaded"
                    )
                    page.wait_for_timeout(1000)

                html = page.content()
                if idx == 0:
                    homepage_text = html_to_text(html)

                # Extract emails
                for e in EMAIL_RE.findall(html):
                    if not any(j in e.lower() for j in EMAIL_JUNK):
                        collected_emails.add(e.lower())

                # Extract phones from tel: links
                for tel_el in page.locator('a[href^="tel:"]').all():
                    href = tel_el.get_attribute("href") or ""
                    clean_p = re.sub(r"[^\d+()\-.\s]", "", href.replace("tel:", "")).strip()
                    if len(clean_p) >= 7:
                        collected_phones.add(clean_p)

            except Exception:
                continue

        # Set best found values
        if collected_emails:
            data["Email"] = sorted(collected_emails)[0]

        if data["Phone"] == "N/A" and collected_phones:
            data["Phone"] = sorted(collected_phones)[0]

        # Trigger Gemini AI if Email or Phone is still missing
        if (
            (data["Email"] == "N/A" or data["Phone"] == "N/A")
            and client
            and homepage_text
        ):
            print("    [*] Calling Gemini AI to extract missing contacts...")
            ai_res = gemini_extract_contacts(homepage_text)
            if data["Email"] == "N/A" and ai_res.get("email") != "N/A":
                data["Email"] = ai_res["email"]
            if data["Phone"] == "N/A" and ai_res.get("phone") != "N/A":
                data["Phone"] = ai_res["phone"]
            if ai_res.get("socials"):
                data["Socials"] = ai_res["socials"]

    except Exception as e:
        print(f"    [!] Website Crawl Error ({website_url}): {e}")
    finally:
        if page:
            page.close()

    return data


def append_lead(lead: dict, filepath: str = "leads_output.csv"):
    """Appends scraped data directly to leads_output.csv."""
    file_exists = os.path.isfile(filepath)
    fieldnames = [
        "Company Name",
        "Category",
        "City",
        "Phone Number",
        "Website",
        "Email/Gmail",
        "Social Links",
    ]
    with open(filepath, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(lead)


def mark_done(index: int, filepath: str = "leads_input.csv"):
    """Updates row status in input queue file."""
    df = pd.read_csv(filepath)
    df.at[index, "Status"] = "done"
    df.to_csv(filepath, index=False)


def run_agent():
    input_file = "leads_input.csv"
    if not os.path.exists(input_file):
        print(f"[!] Input queue file '{input_file}' not found.")
        return

    df = pd.read_csv(input_file)
    pending = df[df["Status"].str.lower() == "pending"]

    if pending.empty:
        print("[*] No pending tasks found in queue.")
        return

    print(f"[*] Processing {len(pending)} search tasks...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for idx, row in pending.iterrows():
            specialty = row["Specialty"]
            city = row["City"]
            query = f"{specialty} in {city}"
            print(f"\n==========================================")
            print(f"[+] Task {idx + 1}: Searching '{query}'")
            print(f"==========================================")

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                locale="en-US",
            )
            maps_page = context.new_page()
            search_page = context.new_page()

            try:
                maps_page.goto(
                    f"https://www.google.com/maps/search/{query.replace(' ', '+')}",
                    timeout=60000,
                )
                maps_page.wait_for_timeout(3000)

                # Consent Dismissal
                if "consent.google" in maps_page.url:
                    for btn_txt in ["Reject all", "Accept all", "I agree"]:
                        if (
                            maps_page.locator(
                                f'button:has-text("{btn_txt}")'
                            ).count()
                            > 0
                        ):
                            maps_page.locator(
                                f'button:has-text("{btn_txt}")'
                            ).first.click()
                            maps_page.wait_for_timeout(2000)
                            break

                feed = maps_page.locator('div[role="feed"]')
                if feed.count() == 0:
                    feed = maps_page.locator('div[aria-label*="Results for"]')

                if feed.count() == 0:
                    print("  [-] No search results panel detected on Maps.")
                    mark_done(idx, input_file)
                    context.close()
                    continue

                # Scroll results list
                for _ in range(4):
                    feed.evaluate("el => el.scrollBy(0, 1000)")
                    maps_page.wait_for_timeout(1500)

                listing_cards = maps_page.locator('a[href*="/maps/place/"]')
                total = min(listing_cards.count(), 10)
                print(f"  [*] Found {total} listings on Google Maps.")

                for i in range(total):
                    try:
                        # Re-query elements dynamically to avoid stale elements
                        card = maps_page.locator('a[href*="/maps/place/"]').nth(
                            i
                        )
                        card.click(timeout=8000)
                        maps_page.wait_for_timeout(2000)

                        # Extract Name
                        name = (
                            card.get_attribute("aria-label")
                            or maps_page.locator("h1").first.inner_text()
                            or "N/A"
                        ).strip()

                        # Extract Phone from Maps side panel
                        phone = "N/A"
                        phone_el = maps_page.locator(
                            'button[data-item-id^="phone:tel:"]'
                        )
                        if phone_el.count() > 0:
                            phone = (
                                phone_el.first.get_attribute("data-item-id")
                                or ""
                            ).replace("phone:tel:", "").strip() or "N/A"

                        if phone == "N/A":
                            phone_alt = maps_page.locator(
                                'button[aria-label^="Phone:"]'
                            )
                            if phone_alt.count() > 0:
                                phone = (
                                    phone_alt.first.get_attribute("aria-label")
                                    or ""
                                ).replace("Phone:", "").strip() or "N/A"

                        # Extract Website from Maps side panel
                        website = "N/A"
                        web_el = maps_page.locator(
                            'a[data-item-id="authority"]'
                        )
                        if web_el.count() > 0:
                            website = (
                                web_el.first.get_attribute("href") or "N/A"
                            )

                        # IF NO WEBSITE ON MAPS: Search Google / DuckDuckGo
                        if website == "N/A":
                            website = search_fallback_website(
                                search_page, name, city
                            )

                        # Deep Crawl Website + Contact Pages
                        crawl = deep_crawl_website(context, website, phone)

                        lead_record = {
                            "Company Name": name,
                            "Category": specialty,
                            "City": city,
                            "Phone Number": crawl["Phone"],
                            "Website": website,
                            "Email/Gmail": crawl["Email"],
                            "Social Links": (
                                ", ".join(crawl["Socials"])
                                if crawl["Socials"]
                                else "N/A"
                            ),
                        }

                        append_lead(lead_record)
                        print(
                            f"  [✓] Lead Saved: {name} | Phone: {crawl['Phone']} | Email: {crawl['Email']}"
                        )

                    except Exception as listing_err:
                        print(f"  [!] Listing Error ({i+1}): {listing_err}")
                        continue

                mark_done(idx, input_file)

            except Exception as task_err:
                print(f"  [!] Task Failed ({query}): {task_err}")
            finally:
                context.close()

        browser.close()


if __name__ == "__main__":
    run_agent()
