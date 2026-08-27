import os
import re
import sys
import time
import pandas as pd
from urllib.parse import urlparse, urljoin, unquote
from playwright.sync_api import sync_playwright

print("\n=============================================", flush=True)
print("   STABLE ZERO-BLEED LEAD SCRAPER ENGINE     ", flush=True)
print("=============================================\n", flush=True)

INPUT_FILE_XLSX = "leads_input.xlsx"
INPUT_FILE_CSV = "leads_input.csv"
OUTPUT_FILE = "leads_output.xlsx"
OUTPUT_CSV_FILE = "leads_output.csv"

BATCH_SIZE = 15 
MAX_RUN_SECONDS = 1500  # 25 minutes limit (safely under 30-min GitHub ceiling)
HEADLESS_MODE = os.environ.get("HEADLESS", "true").lower() == "true"

EMAIL_BLACKLIST_EXTENSIONS = (
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.css', '.js', '.woff', '.ttf'
)
EMAIL_BLACKLIST_DOMAINS = (
    'sentry.io', 'wixpress.com', 'domain.com', 'example.com', 'schema.org', 'gravatar.com'
)


def get_clean_domain(url):
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return None


def handle_google_consent(page):
    try:
        consent_buttons = page.locator('button:has-text("Accept all"), button:has-text("I agree"), form[action*="consent"] button')
        if consent_buttons.count() > 0:
            consent_buttons.first.click(timeout=3000)
            page.wait_for_timeout(1000)
    except Exception:
        pass


def duckduckgo_search_fallback(page, company_name, city):
    try:
        search_query = f"{company_name} {city} website"
        page.goto("https://lite.duckduckgo.com/lite/", timeout=10000, wait_until="domcontentloaded")
        page.fill('input[name="q"]', search_query, timeout=3000)
        page.click('input[type="submit"]', timeout=3000)
        page.wait_for_timeout(1000)
        
        links = page.locator('a[href]').all()
        for link in links[:5]:
            href = link.get_attribute("href") or ""
            if "uddg=" in href:
                match = re.search(r'uddg=([^&]+)', href)
                if match:
                    href = unquote(match.group(1))

            href_lower = href.lower()
            if href.startswith("http") and not any(k in href_lower for k in [
                "duckduckgo.com", "google.com", "youtube.com", "facebook.com", "instagram.com", 
                "yelp.com", "linkedin.com", "twitter.com", "x.com", "tripadvisor.com"
            ]):
                return href
    except Exception:
        pass
    return "N/A"


def deep_crawl_website(context, website_url, existing_phone):
    data = {
        "Email": "N/A", 
        "Phone": existing_phone if existing_phone else "N/A", 
        "Facebook": "N/A", 
        "Instagram": "N/A", 
        "LinkedIn": "N/A", 
        "Twitter": "N/A"
    }

    if not website_url or website_url == "N/A" or "google.com" in website_url:
        return data

    if not website_url.startswith("http"):
        website_url = "https://" + website_url

    page = None
    try:
        page = context.new_page()
        page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"
        })

        try:
            page.goto(website_url, timeout=8000, wait_until="domcontentloaded")
            page.wait_for_timeout(500)
        except Exception:
            return data

        domain = get_clean_domain(website_url)
        pages_to_visit = [website_url]

        try:
            anchors = page.locator("a[href]").all()
            subpage_urls = set()
            for a in anchors[:30]:
                href = a.get_attribute("href") or ""
                href_lower = href.lower()
                if any(term in href_lower for term in ["contact", "about", "info", "reach"]):
                    full_sub_url = urljoin(website_url, href)
                    if domain and domain in (get_clean_domain(full_sub_url) or ""):
                        subpage_urls.add(full_sub_url)
            pages_to_visit.extend(list(subpage_urls)[:1])
        except Exception:
            pass

        for target_url in pages_to_visit:
            if target_url != website_url:
                try:
                    page.goto(target_url, timeout=6000, wait_until="domcontentloaded")
                    page.wait_for_timeout(500)
                except Exception:
                    continue

            if data["Email"] == "N/A":
                try:
                    mailto_links = page.locator('a[href^="mailto:"]').all()
                    for mailto in mailto_links:
                        raw_href = mailto.get_attribute("href") or ""
                        clean_email = raw_href.replace("mailto:", "").split("?")[0].strip()
                        if clean_email and "@" in clean_email:
                            data["Email"] = clean_email
                            break
                except Exception:
                    pass

            if data["Email"] == "N/A":
                raw_html = page.content()
                scraped_emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', raw_html)
                for email in scraped_emails:
                    email_lower = email.lower()
                    if any(email_lower.endswith(ext) for ext in EMAIL_BLACKLIST_EXTENSIONS):
                        continue
                    if any(b_domain in email_lower for b_domain in EMAIL_BLACKLIST_DOMAINS):
                        continue
                    data["Email"] = email
                    break

            try:
                links = page.locator("a[href]").all()
                for link in links:
                    href = link.get_attribute("href") or ""
                    href_lower = href.lower()
                    if "facebook.com" in href_lower and data["Facebook"] == "N/A":
                        data["Facebook"] = href
                    elif "instagram.com" in href_lower and data["Instagram"] == "N/A":
                        data["Instagram"] = href
                    elif "linkedin.com" in href_lower and data["LinkedIn"] == "N/A":
                        data["LinkedIn"] = href
                    elif any(t in href_lower for t in ["twitter.com", "x.com"]) and data["Twitter"] == "N/A":
                        data["Twitter"] = href
            except Exception:
                pass

    except Exception:
        pass
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass

    return data


def save_leads_to_disk(all_output_rows, output_columns):
    all_df = pd.DataFrame(all_output_rows, columns=output_columns)
    all_df.to_csv(OUTPUT_CSV_FILE, index=False)
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        all_df.to_excel(writer, sheet_name="All Data", index=False)


def run_pipeline():
    start_time = time.time()
    output_columns = [
        "Searched Specialty", "Searched City", "Company Name", "Phone Number",
        "Website", "Email/Gmail", "Facebook", "Instagram", "LinkedIn", "Twitter/X"
    ]

    all_output_rows = []
    completed_tasks = set()

    if os.path.exists(OUTPUT_FILE):
        try:
            existing_df = pd.read_excel(OUTPUT_FILE)
            all_output_rows = existing_df.to_dict("records")
            for r in all_output_rows:
                spec = str(r.get("Searched Specialty", "")).strip().lower()
                city = str(r.get("Searched City", "")).strip().lower()
                if spec and city:
                    completed_tasks.add(f"{spec}|{city}")
            print(f"[+] Loaded {len(all_output_rows)} existing leads across {len(completed_tasks)} finished cities.", flush=True)
        except Exception as e:
            print(f"[!] Notice: Creating fresh output file ({e})", flush=True)

    if os.path.exists(INPUT_FILE_XLSX):
        df_tasks = pd.read_excel(INPUT_FILE_XLSX)
    elif os.path.exists(INPUT_FILE_CSV):
        df_tasks = pd.read_csv(INPUT_FILE_CSV)
    else:
        print("[-] Input file not found!", flush=True)
        sys.exit(1)

    df_tasks.columns = [str(c).strip().capitalize() for c in df_tasks.columns]
    specialty_col = 'Speciality' if 'Speciality' in df_tasks.columns else 'Specialty'

    seen_names = {str(r.get("Company Name", "")).strip() for r in all_output_rows if r.get("Company Name")}

    tasks_to_run = []
    for idx, row in df_tasks.iterrows():
        spec = str(row[specialty_col]).strip()
        city = str(row['City']).strip()
        if not spec or not city or pd.isna(spec) or pd.isna(city):
            continue
        task_key = f"{spec.lower()}|{city.lower()}"
        if task_key not in completed_tasks:
            tasks_to_run.append((spec, city))

    total_pending = len(tasks_to_run)
    if total_pending == 0:
        print("[+++] ALL CITIES COMPLETED! No remaining tasks in queue.", flush=True)
        return

    print(f"[+] Total Remaining Cities: {total_pending}", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS_MODE,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
        )

        for task_idx, (target_specialty, target_city) in enumerate(tasks_to_run):
            if (time.time() - start_time) > MAX_RUN_SECONDS:
                print("[!] Reached 25-minute safety threshold. Exiting batch gracefully to hand off to next job...", flush=True)
                break

            search_query = f"{target_specialty} in {target_city}"
            print(f"[*] CITY [{task_idx + 1}/{total_pending}]: {search_query.upper()}", flush=True)

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            maps_page = context.new_page()
            search_page = context.new_page()

            clean_url = f"https://www.google.com/maps/search/{search_query.replace(' ', '+')}"

            try:
                maps_page.goto(clean_url, wait_until="domcontentloaded", timeout=15000)
                handle_google_consent(maps_page)
            except Exception:
                pass

            listings_feed = maps_page.locator('div[role="feed"]')
            if listings_feed.count() == 0:
                listings_feed = maps_page.locator('div[aria-label*="Results for"]')

            scroll_attempts = 0
            last_count = 0
            strikes = 0
            while scroll_attempts < 10:
                scroll_attempts += 1
                try:
                    listings_feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                except Exception:
                    maps_page.keyboard.press("PageDown")

                maps_page.wait_for_timeout(800)
                current_count = maps_page.locator('a[href*="/maps/place/"]').count()
                
                if current_count == last_count:
                    strikes += 1
                    if strikes >= 2:
                        break
                else:
                    strikes = 0

                if current_count >= 20:
                    break
                last_count = current_count

            cards = maps_page.locator('a[href*="/maps/place/"]').all()
            current_city_leads = []

            for card in cards[:20]:
                try:
                    raw_name = card.get_attribute("aria-label") or "Unknown Business"
                    name = raw_name.strip()
                    if name in seen_names or name == "Unknown Business":
                        continue

                    card.click(timeout=3000)
                    maps_page.wait_for_timeout(1000)

                    phone = "N/A"
                    website = "N/A"

                    web_el = maps_page.locator('a[data-item-id="authority"]')
                    if web_el.count() > 0:
                        website = web_el.get_attribute("href") or "N/A"

                    phone_el = maps_page.locator('button[data-item-id^="phone:tel:"]')
                    if phone_el.count() > 0:
                        phone = phone_el.get_attribute("data-item-id").replace("phone:tel:", "").strip()

                    current_city_leads.append({
                        "Target Specialty": target_specialty,
                        "Target City": target_city,
                        "Company Name": name,
                        "Phone Number": phone,
                        "Website": website
                    })
                    seen_names.add(name)
                except Exception:
                    continue

            if current_city_leads:
                for base_lead in current_city_leads:
                    if base_lead["Website"] in ["N/A", ""]:
                        base_lead["Website"] = duckduckgo_search_fallback(search_page, base_lead["Company Name"], base_lead["Target City"])

                    crawl_data = deep_crawl_website(context, base_lead["Website"], base_lead["Phone Number"])

                    row_data = {
                        "Searched Specialty": base_lead["Target Specialty"],
                        "Searched City": base_lead["Target City"],
                        "Company Name": base_lead["Company Name"],
                        "Phone Number": crawl_data["Phone"],
                        "Website": base_lead["Website"],
                        "Email/Gmail": crawl_data["Email"],
                        "Facebook": crawl_data["Facebook"],
                        "Instagram": crawl_data["Instagram"],
                        "LinkedIn": crawl_data["LinkedIn"],
                        "Twitter/X": crawl_data["Twitter"]
                    }
                    all_output_rows.append(row_data)

            context.close()
            save_leads_to_disk(all_output_rows, output_columns)

        browser.close()

    print(f"\n[+] BATCH FINISHED SAFE. Saved total of {len(all_output_rows)} leads into '{OUTPUT_FILE}'.", flush=True)


if __name__ == "__main__":
    run_pipeline()
