"""CLI to scrape OEM parts catalog from parts.lexus.com (SimplePart platform).

Uses Playwright (headless Chrome) to bypass Cloudflare bot protection.
Scrapes part numbers, names, prices, and diagram references into the parts_catalog table.
Run once per vehicle to populate the local parts database.

Site structure (SimplePart):
  Vehicle page:       /Lexus_2017_GX-460.html
  Trim page:          /Lexus_2017_GX-460-Base.html   (lists categories)
  Category page:      /Lexus_2017_GX-460-Base/Transmission-and-Driveline.html  (lists /t/ links)
  Part-type page:     /t/Lexus_2017_GX-460-Base/VALVE--SOLENOIDSLT.html  (has /p/ links + prices)
  Individual part:    /p/Lexus_2017_GX-460/Part-Name/DiagramID/PartNumber.html

Usage:
    python -m backend.cli.scrape_parts --make Lexus --model GX-460 --year 2017
    python -m backend.cli.scrape_parts --make Lexus --model GX-460 --year 2017 --category Transmission-and-Driveline
    python -m backend.cli.scrape_parts --make Lexus --model GX-460 --year 2017 --no-headless
    python -m backend.cli.scrape_parts --make Lexus --model GX-460 --year 2017 --dry-run
"""

from __future__ import annotations

import json
import re
import time
import uuid

import click
from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from sqlalchemy import create_engine, text

from backend.app.core.config import settings

console = Console()

BASE_URL = "https://parts.lexus.com"
REQUEST_DELAY = 2.0  # seconds — be respectful


class Browser:
    """Wrapper around Playwright headless Chrome for scraping."""

    def __init__(self, cookies_file: str | None = None, headless: bool = True):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(self._context)
        except ImportError:
            pass
        if cookies_file:
            self._load_cookies(cookies_file)
        self._page = self._context.new_page()

    def _load_cookies(self, path: str):
        with open(path) as f:
            raw = json.load(f)
        cookies = raw if isinstance(raw, list) else raw.get("cookies", [])
        same_site_map = {
            "no_restriction": "None", "unspecified": "Lax",
            "lax": "Lax", "strict": "Strict", "none": "None",
        }
        cleaned = []
        for c in cookies:
            cookie = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".parts.lexus.com"),
                "path": c.get("path", "/"),
            }
            if c.get("secure"):
                cookie["secure"] = True
            if c.get("httpOnly"):
                cookie["httpOnly"] = True
            raw_ss = (c.get("sameSite") or "Lax").lower()
            cookie["sameSite"] = same_site_map.get(raw_ss, "Lax")
            cleaned.append(cookie)
        self._context.add_cookies(cleaned)
        console.print(f"[dim]Loaded {len(cleaned)} cookies[/dim]")

    def fetch(self, url: str) -> BeautifulSoup | None:
        """Navigate to URL, wait for content, return parsed HTML."""
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self._page.wait_for_timeout(3000)

            for _ in range(5):
                html = self._page.content()
                if "Attention Required" not in html and "challenge-platform" not in html:
                    break
                console.print("[dim]Waiting for Cloudflare challenge to clear...[/dim]")
                self._page.wait_for_timeout(3000)

            html = self._page.content()
            soup = BeautifulSoup(html, "html.parser")
            title = soup.find("title")
            if title and "Attention Required" in (title.text or ""):
                console.print(f"[red]Cloudflare blocked: {url}[/red]")
                console.print("[yellow]Try --no-headless to solve the challenge manually, or provide fresh cookies.[/yellow]")
                return None
            self._save_cf_cookies()
            return soup
        except Exception as e:
            console.print(f"[red]Failed to fetch {url}: {e}[/red]")
            return None

    def _save_cf_cookies(self):
        try:
            cookies = self._context.cookies()
            cf_cookies = [c for c in cookies if "cf" in c["name"].lower() or "clearance" in c["name"].lower()]
            if cf_cookies:
                with open("cookies_cf_auto.json", "w") as f:
                    json.dump(cookies, f, indent=2)
        except Exception:
            pass

    def close(self):
        self._browser.close()
        self._pw.stop()


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _get_vehicle_url(make: str, model: str, year: int | None) -> str:
    if year:
        return f"{BASE_URL}/{make}_{year}_{model}.html"
    return f"{BASE_URL}/{make}__{model}.html"


# ---------------------------------------------------------------------------
# Step 1: Vehicle page → find trim links
# ---------------------------------------------------------------------------

def _scrape_trims(soup: BeautifulSoup, make: str, model: str, year: int | None) -> list[dict]:
    """Find trim links like /Lexus_2017_GX-460-Base.html on the vehicle page."""
    vehicle_slug = f"{make}_{year}_{model}" if year else f"{make}__{model}"
    trims = []
    seen = set()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        # Trim links: start with vehicle slug + dash + trim name, ending in .html
        # e.g. /Lexus_2017_GX-460-Base.html  (but NOT /Lexus_2017_GX-460.html itself)
        if not href.endswith(".html"):
            continue
        # Normalize
        path = href.split("?")[0]
        # Must contain the vehicle slug with a trim suffix
        basename = path.rstrip("/").rsplit("/", 1)[-1].replace(".html", "")
        if not basename.startswith(vehicle_slug + "-"):
            continue
        trim_name = basename[len(vehicle_slug) + 1:]  # e.g. "Base", "Premium"
        if not trim_name or trim_name in seen:
            continue
        seen.add(trim_name)
        full_url = f"{BASE_URL}{path}" if path.startswith("/") else path
        trims.append({
            "name": link.get_text(strip=True) or trim_name,
            "slug": basename,
            "url": full_url,
        })
    return trims


# ---------------------------------------------------------------------------
# Step 2: Trim page → find category links
# ---------------------------------------------------------------------------

def _scrape_categories(soup: BeautifulSoup, trim_slug: str) -> list[dict]:
    """Find category links on a trim page, e.g. /Lexus_2017_GX-460-Base/Transmission-and-Driveline.html"""
    categories = []
    seen = set()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not href.endswith(".html"):
            continue
        path = href.split("?")[0]
        # Must be under the trim slug directory
        # e.g. /Lexus_2017_GX-460-Base/Transmission-and-Driveline.html
        if f"/{trim_slug}/" not in path:
            continue
        cat_name_raw = path.rstrip("/").rsplit("/", 1)[-1].replace(".html", "")
        if not cat_name_raw or cat_name_raw in seen:
            continue
        # Skip /t/ and /p/ links — those are part-type and individual part pages
        if "/t/" in path or "/p/" in path:
            continue
        seen.add(cat_name_raw)
        full_url = f"{BASE_URL}{path}" if path.startswith("/") else path
        categories.append({
            "name": link.get_text(strip=True) or cat_name_raw.replace("-", " "),
            "slug": cat_name_raw,
            "url": full_url,
        })
    return categories


# ---------------------------------------------------------------------------
# Step 3: Category page → find /t/ links (OEM part-type pages)
# ---------------------------------------------------------------------------

def _scrape_part_type_links(soup: BeautifulSoup) -> list[dict]:
    """Find /t/ links on a category page. These are the OEM part-type pages."""
    links = []
    seen = set()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/t/" not in href:
            continue
        path = href.split("?")[0]
        if path in seen:
            continue
        seen.add(path)
        # Extract part-type name from the URL
        # e.g. /t/Lexus_2017_GX-460-Base/VALVE--SOLENOIDSLT.html → VALVE--SOLENOIDSLT
        basename = path.rstrip("/").rsplit("/", 1)[-1].replace(".html", "")
        full_url = f"{BASE_URL}{path}" if path.startswith("/") else path
        links.append({
            "name": link.get_text(strip=True) or basename.replace("--", ", ").replace("-", " "),
            "slug": basename,
            "url": full_url,
        })
    return links


# ---------------------------------------------------------------------------
# Step 4: /t/ page → extract parts (P/N from /p/ links, prices from page)
# ---------------------------------------------------------------------------

def _scrape_parts_from_type_page(soup: BeautifulSoup, category: str, subcategory: str) -> list[dict]:
    """Extract parts from a /t/ part-type page.

    Parts appear as /p/ links with structure:
        /p/Lexus_2017_GX-460/Part-Name/DiagramID/PartNumber.html
    Prices appear near the parts, typically in elements with price-related classes.
    """
    parts = []
    seen_pns = set()

    # Find all /p/ links which contain the actual part numbers
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/p/" not in href:
            continue
        path = href.split("?")[0].replace(".html", "").rstrip("/")
        segments = path.split("/")
        # Expected: ['', 'p', 'Lexus_2017_GX-460', 'Part-Name', 'DiagramID', 'PartNumber']
        if len(segments) < 4:
            continue
        part_number = segments[-1]
        # Toyota/Lexus OEM part numbers are typically 10 alphanumeric chars
        if not re.match(r'^[A-Z0-9]{5,}$', part_number, re.I):
            continue
        if part_number in seen_pns:
            continue
        seen_pns.add(part_number)

        diagram_id = segments[-2] if len(segments) >= 5 else None
        link_text = link.get_text(strip=True)

        # Derive part name: prefer link text, then URL path segment, then subcategory
        if link_text and link_text != part_number and not re.match(r'^[A-Z0-9]{5,}$', link_text):
            part_name = link_text
        elif len(segments) >= 5:
            part_name = _slug_to_name(segments[-3])
        else:
            part_name = subcategory or part_number

        # Try to find the price near this link element
        price = _find_nearby_price(link)

        full_url = f"{BASE_URL}{href}" if href.startswith("/") else href
        parts.append({
            "oem_part_number": part_number,
            "part_name": part_name,
            "msrp": price,
            "diagram_id": diagram_id,
            "source_url": full_url,
            "category": category,
            "subcategory": subcategory,
        })

    # If we found /p/ links but no prices, try global price extraction
    if parts and not any(p.get("msrp") for p in parts):
        prices = _extract_all_prices(soup)
        # If we have matching counts, assign by order
        if len(prices) == len(parts):
            for p, price in zip(parts, prices):
                p["msrp"] = price

    return parts


def _find_nearby_price(link_element) -> float | None:
    """Walk up the DOM from a /p/ link to find a nearby price."""
    # Check siblings and parent containers for price text
    parent = link_element.parent
    for _ in range(4):  # Walk up to 4 levels
        if parent is None:
            break
        # Look for price elements within this container
        price_el = parent.find(class_=re.compile(r"price|msrp|cost", re.I))
        if price_el:
            price = _parse_price(price_el.get_text(strip=True))
            if price:
                return price
        # Also check for price in the raw text
        text = parent.get_text(" ", strip=True)
        price = _parse_price(text)
        if price:
            return price
        parent = parent.parent
    return None


def _extract_all_prices(soup: BeautifulSoup) -> list[float]:
    """Extract all prices from page as a fallback."""
    prices = []
    for el in soup.find_all(class_=re.compile(r"price|msrp", re.I)):
        p = _parse_price(el.get_text(strip=True))
        if p:
            prices.append(p)
    return prices


def _parse_price(text: str) -> float | None:
    match = re.search(r'\$?([\d,]+\.\d{2})', text)
    if match:
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _slug_to_name(slug: str) -> str:
    """Convert a URL slug to a readable name."""
    return slug.replace("--", ", ").replace("-", " ").title()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _store_parts(parts: list[dict], make: str, model: str, year_start: int, year_end: int,
                  region: str, currency: str, price_type: str, engine):
    if not parts:
        return 0

    stored = 0
    with engine.connect() as conn:
        for p in parts:
            existing = conn.execute(
                text("SELECT 1 FROM parts_catalog WHERE oem_part_number = :pn AND make = :make AND model = :model AND region = :region LIMIT 1"),
                {"pn": p["oem_part_number"], "make": make, "model": model, "region": region},
            ).fetchone()

            if existing:
                if p.get("msrp"):
                    conn.execute(
                        text("UPDATE parts_catalog SET msrp = :msrp, scraped_at = NOW() WHERE oem_part_number = :pn AND make = :make AND model = :model AND region = :region"),
                        {"msrp": p["msrp"], "pn": p["oem_part_number"], "make": make, "model": model, "region": region},
                    )
                continue

            conn.execute(
                text("""
                    INSERT INTO parts_catalog
                        (id, make, model, year_start, year_end, oem_part_number, part_name,
                         description, category, subcategory, diagram_id, diagram_url,
                         callout_number, msrp, currency, region, price_type,
                         non_reusable, source_url)
                    VALUES
                        (:id, :make, :model, :ys, :ye, :pn, :name,
                         :desc, :cat, :subcat, :diag_id, :diag_url,
                         :callout, :msrp, :currency, :region, :price_type,
                         :non_reusable, :source_url)
                """),
                {
                    "id": str(uuid.uuid4()),
                    "make": make, "model": model,
                    "ys": year_start, "ye": year_end,
                    "pn": p["oem_part_number"],
                    "name": p.get("part_name", ""),
                    "desc": p.get("description"),
                    "cat": p.get("category"),
                    "subcat": p.get("subcategory"),
                    "diag_id": p.get("diagram_id"),
                    "diag_url": p.get("diagram_url"),
                    "callout": p.get("callout_number"),
                    "msrp": p.get("msrp"),
                    "currency": currency,
                    "region": region,
                    "price_type": price_type,
                    "non_reusable": p.get("non_reusable", False),
                    "source_url": p.get("source_url"),
                },
            )
            stored += 1
        conn.commit()
    return stored


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--make", required=True, help="Vehicle make (e.g. Lexus)")
@click.option("--model", required=True, help="Vehicle model as shown on parts site (e.g. GX-460)")
@click.option("--year", default=None, type=int, help="Model year (e.g. 2017)")
@click.option("--year-start", default=None, type=int, help="First year for storage range (defaults to --year)")
@click.option("--year-end", default=None, type=int, help="Last year for storage range (defaults to --year)")
@click.option("--category", default=None, help="Scrape only this category slug (e.g. Transmission-and-Driveline)")
@click.option("--region", default="US", help="Price region (e.g. US, CA)")
@click.option("--currency", default="USD", help="Currency code (e.g. USD, CAD)")
@click.option("--price-type", default="MSRP", help="Price type (e.g. MSRP, dealer, retail)")
@click.option("--cookies", default=None, type=click.Path(exists=True), help="Path to cookies JSON file")
@click.option("--no-headless", is_flag=True, help="Show browser window (needed for Cloudflare)")
@click.option("--dry-run", is_flag=True, help="Show what would be scraped without storing")
def scrape_parts(make: str, model: str, year: int | None, year_start: int | None,
                 year_end: int | None, category: str | None, region: str, currency: str,
                 price_type: str, cookies: str | None, no_headless: bool, dry_run: bool):
    """Scrape OEM parts catalog from parts.lexus.com into the local database."""

    ys = year_start or year or 2016
    ye = year_end or year or 2021

    console.print(f"\n[bold]Parts Catalog Scraper[/bold]")
    console.print(f"[bold]Vehicle:[/bold] {make} {model} ({ys}-{ye})")
    console.print(f"[bold]Source:[/bold] {BASE_URL}")
    console.print(f"[bold]Region:[/bold] {region} | [bold]Currency:[/bold] {currency} | [bold]Price type:[/bold] {price_type}")
    if category:
        console.print(f"[bold]Category filter:[/bold] {category}")
    console.print()

    browser = Browser(cookies_file=cookies, headless=not no_headless)
    engine = create_engine(settings.postgres_sync_url) if not dry_run else None

    try:
        _run_scrape(browser, engine, make, model, year, ys, ye, category,
                    region, currency, price_type, dry_run)
    finally:
        browser.close()
        if engine:
            engine.dispose()


def _run_scrape(browser: Browser, engine, make: str, model: str, year: int | None,
                ys: int, ye: int, category: str | None,
                region: str, currency: str, price_type: str, dry_run: bool):
    """Main scrape logic."""

    # Step 1: Fetch vehicle page and find trims
    vehicle_url = _get_vehicle_url(make, model, year)
    console.print(f"[dim]Fetching vehicle page: {vehicle_url}[/dim]")
    soup = browser.fetch(vehicle_url)
    if not soup:
        console.print("[red]Could not load vehicle page. Check make/model/year.[/red]")
        return

    title = soup.find("title")
    console.print(f"[dim]Page title: {title.text.strip() if title else 'none'}[/dim]\n")

    trims = _scrape_trims(soup, make, model, year)
    if not trims:
        console.print("[yellow]No trim links found on vehicle page.[/yellow]")
        console.print("[dim]Try --no-headless to see what the browser loads.[/dim]")
        return

    console.print(f"Found [bold]{len(trims)}[/bold] trim(s):")
    for t in trims:
        console.print(f"  [dim]{t['slug']}[/dim] — {t['name']}")
    console.print()

    all_parts = []

    for trim in trims:
        console.print(f"\n[bold]Scraping trim: {trim['name']}[/bold]")
        time.sleep(REQUEST_DELAY)

        # Step 2: Fetch trim page and find categories
        trim_soup = browser.fetch(trim["url"])
        if not trim_soup:
            console.print(f"  [red]Could not load trim page[/red]")
            continue

        categories = _scrape_categories(trim_soup, trim["slug"])

        # Also check: the vehicle page itself may list categories under the trim
        # Some SimplePart sites skip the dedicated trim page
        if not categories:
            categories = _scrape_categories(soup, trim["slug"])

        if not categories:
            console.print(f"  [yellow]No categories found for {trim['name']}[/yellow]")
            continue

        if category:
            categories = [c for c in categories if c["slug"].lower() == category.lower()]
            if not categories:
                all_cat_slugs = [c["slug"] for c in _scrape_categories(trim_soup, trim["slug"])]
                console.print(f"  [red]Category '{category}' not found. Available: {', '.join(all_cat_slugs[:10])}[/red]")
                continue

        console.print(f"  Found [bold]{len(categories)}[/bold] categories")

        # Step 3: For each category, find /t/ part-type links
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      BarColumn(), TaskProgressColumn()) as progress:
            cat_task = progress.add_task("Categories...", total=len(categories))

            for cat in categories:
                progress.update(cat_task, description=f"  {cat['name']}")
                time.sleep(REQUEST_DELAY)

                cat_soup = browser.fetch(cat["url"])
                if not cat_soup:
                    progress.advance(cat_task)
                    continue

                part_type_links = _scrape_part_type_links(cat_soup)

                if not part_type_links:
                    # The category page itself might have parts directly
                    parts = _scrape_parts_from_type_page(cat_soup, cat["name"], "")
                    if parts:
                        all_parts.extend(parts)
                        console.print(f"    [dim]{cat['name']}: {len(parts)} parts (direct)[/dim]")
                    progress.advance(cat_task)
                    continue

                # Step 4: Scrape each /t/ part-type page
                for pt in part_type_links:
                    time.sleep(REQUEST_DELAY)
                    pt_soup = browser.fetch(pt["url"])
                    if not pt_soup:
                        continue
                    parts = _scrape_parts_from_type_page(pt_soup, cat["name"], pt["name"])
                    if parts:
                        all_parts.extend(parts)
                        console.print(f"    [dim]{pt['name']}: {len(parts)} parts[/dim]")

                progress.advance(cat_task)

    # Deduplicate by part number
    seen_pns = set()
    unique_parts = []
    for p in all_parts:
        pn = p["oem_part_number"]
        if pn not in seen_pns:
            seen_pns.add(pn)
            unique_parts.append(p)

    console.print(f"\nScraped [bold]{len(unique_parts)}[/bold] unique parts ({len(all_parts)} total with duplicates)\n")

    # Step 5: Display or store
    if dry_run:
        console.print("[yellow]Dry run — showing first 50 parts:[/yellow]\n")
        for p in unique_parts[:50]:
            price = f"${p['msrp']:.2f}" if p.get("msrp") else "no price"
            console.print(f"  {p['oem_part_number']:>15} | {p.get('part_name', ''):50} | {price:>10} | {p.get('category', '')}")
        if len(unique_parts) > 50:
            console.print(f"  ... and {len(unique_parts) - 50} more")
    else:
        console.print("[dim]Storing parts in database...[/dim]")
        model_clean = model.replace("-", "")
        stored = _store_parts(unique_parts, make, model_clean, ys, ye, region, currency, price_type, engine)
        console.print(f"[green]Stored {stored} new parts ({len(unique_parts) - stored} already existed).[/green]")

    console.print("\n[bold green]Done![/bold green]")


if __name__ == "__main__":
    scrape_parts()
