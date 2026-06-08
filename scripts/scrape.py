#!/usr/bin/env python3
"""
Scrape Ignition 8.3 scripting function docs and write vim help files to ../doc/.

Usage:
    pip install requests beautifulsoup4
    python scrape.py
"""

import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://docs.inductiveautomation.com"
ROOT_PATH = "/docs/8.3/appendix/scripting-functions"
OUT_DIR = Path(__file__).parent.parent / "doc"
MAX_WORKERS = 10

_local = threading.local()
_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg)


def get_session() -> requests.Session:
    if not hasattr(_local, "session"):
        s = requests.Session()
        s.headers["User-Agent"] = "ignition-docs.nvim scraper/1.0"
        _local.session = s
    return _local.session


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch(path: str) -> BeautifulSoup:
    url = BASE_URL + path
    log(f"  GET {url}")
    resp = get_session().get(url, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


# ---------------------------------------------------------------------------
# Vim help formatting helpers
# ---------------------------------------------------------------------------

SEP = "-" * 78
WRAP = 78


def tag_anchor(name: str, prefix_len: int = 0) -> str:
    """Pad then append *tag* so the line reaches column WRAP."""
    tag = f"*{name}*"
    pad = max(1, WRAP - prefix_len - len(tag))
    return " " * pad + tag


def wrap_text(text: str, indent: int = 2) -> str:
    """Wrap and indent a block of plain text."""
    words = text.split()
    if not words:
        return ""
    lines = []
    prefix = " " * indent
    line = prefix
    for word in words:
        if len(line) + len(word) + 1 > WRAP:
            lines.append(line.rstrip())
            line = prefix + word + " "
        else:
            line += word + " "
    lines.append(line.rstrip())
    return "\n".join(lines)


def clean(text: str) -> str:
    return " ".join(text.split())


def text_of(element) -> str:
    if element is None:
        return ""
    return clean(element.get_text(" ", strip=True))


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------

def heading_text(element) -> str:
    """Get heading text, stripping zero-width spaces and normalizing."""
    return element.get_text(" ", strip=True).replace("​", "").strip()


def parse_function_page(soup: BeautifulSoup, qualified_name: str) -> dict:
    """Extract structured data from a function detail page."""
    main = soup.find("article") or soup.find("main") or soup.body

    result = {
        "name": qualified_name,
        "description": "",
        "syntaxes": [],   # list of {"signature": str, "params": [...], "returns": str}
        "scope": "",
        "examples": [],
    }

    if main is None:
        return result

    # State machine over flattened element list
    # Sections are introduced by h1/h2/h3 headings.
    current_section = None  # "description" | "syntax" | "params" | "returns" | "scope" | "examples"
    current_syntax = None

    for element in main.find_all(["h1", "h2", "h3", "h4", "table", "pre", "p", "code"]):
        if not isinstance(element, Tag):
            continue

        tag = element.name
        txt_raw = heading_text(element)
        txt = txt_raw.lower()

        # --- Heading: update section state ---
        if tag in ("h1", "h2", "h3", "h4"):
            if "description" in txt:
                current_section = "description"
            elif "syntax" in txt:
                current_section = "syntax"
                current_syntax = {"signature": "", "params": [], "returns": ""}
                result["syntaxes"].append(current_syntax)
            elif "parameter" in txt:
                current_section = "params"
            elif "return" in txt:
                current_section = "returns"
            elif "scope" in txt:
                current_section = "scope"
            elif "example" in txt:
                current_section = "examples"
            elif tag == "h1":
                # The page title — skip, we already have qualified_name
                pass
            else:
                # Unknown heading (e.g. "Client Permission Restrictions") — leave section alone
                pass
            continue

        # --- Signature: standalone <code> block in syntax section ---
        if tag == "code" and current_section == "syntax" and current_syntax is not None:
            code_text = clean(element.get_text(" ", strip=True))
            # Signature looks like "system.something(..." — skip inline param names
            if "system." in code_text and "(" in code_text and not current_syntax["signature"]:
                current_syntax["signature"] = code_text
            continue

        # --- Parameters table ---
        if tag == "table" and current_section == "params" and current_syntax is not None:
            rows = element.find_all("tr")
            for row in rows[1:]:  # skip header row
                cells = row.find_all(["td", "th"])
                if len(cells) >= 3:
                    param_type = clean(cells[0].get_text(" ", strip=True))
                    param_name = clean(cells[1].get_text(" ", strip=True))
                    param_desc = clean(cells[2].get_text(" ", strip=True))
                    current_syntax["params"].append({
                        "type": param_type,
                        "name": param_name,
                        "description": param_desc,
                    })
                elif len(cells) == 2:
                    param_name = clean(cells[0].get_text(" ", strip=True))
                    param_desc = clean(cells[1].get_text(" ", strip=True))
                    current_syntax["params"].append({
                        "type": "",
                        "name": param_name,
                        "description": param_desc,
                    })
            continue

        # --- Code examples: <pre> blocks ---
        if tag == "pre" and current_section == "examples":
            # Lines are in <span class="token-line"> elements; extract each line's text.
            token_lines = element.find_all("span", class_="token-line")
            if token_lines:
                code = "\n".join(
                    span.get_text("", strip=False).rstrip() for span in token_lines
                )
            else:
                # Fallback: replace <br> with newlines manually
                for br in element.find_all("br"):
                    br.replace_with("\n")
                code = element.get_text("", strip=False)
            result["examples"].append(code)
            continue

        # --- Paragraph content ---
        if tag == "p":
            para_text = clean(element.get_text(" ", strip=True))
            if not para_text:
                continue
            if current_section == "description" and not result["description"]:
                result["description"] = para_text
            elif current_section == "scope" and not result["scope"]:
                result["scope"] = para_text
            elif current_section == "returns" and current_syntax is not None and not current_syntax["returns"]:
                current_syntax["returns"] = para_text

    return result


# ---------------------------------------------------------------------------
# Vim help file rendering
# ---------------------------------------------------------------------------

def render_function(data: dict) -> str:
    lines = []
    name = data["name"]
    tag_name = f"ignition-{name}"

    lines.append(SEP)
    # Function heading with tag right-aligned
    heading = f"{name}()"
    tag = f"*{tag_name}*"
    pad = WRAP - len(heading) - len(tag)
    lines.append(f"{heading}{' ' * max(1, pad)}{tag}")
    lines.append("")

    if data["description"]:
        lines.append(wrap_text(data["description"], indent=2))
        lines.append("")

    for syntax in data["syntaxes"]:
        if syntax["signature"]:
            lines.append(f"  Syntax: >")
            lines.append(f"    {syntax['signature']}")
            lines.append(f"<")
            lines.append("")

        if syntax["params"]:
            lines.append(f"  Parameters: ~")
            max_name = max((len(p["name"]) for p in syntax["params"]), default=8)
            max_type = max((len(p["type"]) for p in syntax["params"]), default=6)
            for p in syntax["params"]:
                name_col = f"{{{p['name']}}}".ljust(max_name + 2)
                type_col = (f"({p['type']})".ljust(max_type + 2)) if p["type"] else " " * (max_type + 3)
                prefix = f"    {name_col}  {type_col}  "
                desc_words = p["description"].split()
                desc_lines = []
                current = ""
                for word in desc_words:
                    if len(prefix if not desc_lines else " " * len(prefix)) + len(current) + len(word) + 1 > WRAP:
                        desc_lines.append(current.rstrip())
                        current = word + " "
                    else:
                        current += word + " "
                if current.strip():
                    desc_lines.append(current.rstrip())
                if desc_lines:
                    lines.append(f"{prefix}{desc_lines[0]}")
                    cont = " " * len(prefix)
                    for dl in desc_lines[1:]:
                        lines.append(f"{cont}{dl}")
                else:
                    lines.append(prefix)
            lines.append("")

        if syntax["returns"]:
            lines.append(f"  Returns: ~")
            lines.append(wrap_text(syntax["returns"], indent=4))
            lines.append("")

    if data["scope"]:
        lines.append(f"  Scope: {data['scope']}")
        lines.append("")

    if data["examples"]:
        lines.append(f"  Example: >")
        for ex in data["examples"][:1]:  # include first example only
            for ex_line in ex.splitlines():
                lines.append(f"    {ex_line}")
        lines.append("<")
        lines.append("")

    return "\n".join(lines)


def render_module_file(module_slug: str, qualified_module: str, functions: list[dict]) -> str:
    """Render a complete vim help file for one module."""
    short = module_slug  # e.g. "tag"
    file_tag = f"ignition-{short}.txt"
    module_tag = f"ignition-{qualified_module}"

    lines = []

    # File header
    header_left = f"*{file_tag}*"
    header_right = f"Ignition {qualified_module} functions"
    pad = WRAP - len(header_left) - len(header_right)
    lines.append(f"{header_left}{' ' * max(1, pad)}{header_right}")
    lines.append("")

    # Module section heading
    section_title = qualified_module.upper()
    lines.append(f"{section_title}{tag_anchor(module_tag, len(section_title))}")
    lines.append("")

    # Function index
    for fn in functions:
        fn_tag = f"|ignition-{fn['name']}|"
        lines.append(f"  {fn_tag}")
    lines.append("")

    # Function entries
    for fn in functions:
        lines.append(render_function(fn))

    lines.append("")
    lines.append("vim:tw=78:ft=help:norl:")

    return "\n".join(lines)


def render_index(modules: list[dict]) -> str:
    """Render doc/ignition.txt — the main index file."""
    lines = []

    header_left = "*ignition.txt*"
    header_right = "Ignition 8.3 Scripting Functions"
    pad = WRAP - len(header_left) - len(header_right)
    lines.append(f"{header_left}{' ' * max(1, pad)}{header_right}")
    lines.append("")

    title = "IGNITION SCRIPTING FUNCTIONS"
    lines.append(f"{title}{tag_anchor('ignition', len(title))}")
    lines.append("")
    lines.append(wrap_text(
        "Vim help reference for Ignition 8.3 system scripting functions. "
        "Generated from https://docs.inductiveautomation.com/docs/8.3/appendix/scripting-functions",
        indent=0,
    ))
    lines.append("")

    mod_heading = "MODULES"
    lines.append(f"{mod_heading}{tag_anchor('ignition-modules', len(mod_heading))}")
    lines.append("")

    for mod in modules:
        link = f"|ignition-{mod['qualified']}|"
        desc = mod["qualified"]
        lines.append(f"  {link:<45}  {desc}")

    lines.append("")
    lines.append("vim:tw=78:ft=help:norl:")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_modules(soup: BeautifulSoup) -> list[dict]:
    """
    Find all module links on the main scripting-functions page.
    Returns list of {"slug": "system-tag", "qualified": "system.tag", "path": "/docs/..."}
    """
    modules = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Match links that are direct children of the scripting-functions path
        m = re.match(
            r"^(/docs/8\.3/appendix/scripting-functions/)(system-[\w]+)$",
            href.split("?")[0].rstrip("/"),
        )
        if m and m.group(2) not in seen:
            slug = m.group(2)  # e.g. "system-tag"
            qualified = slug.replace("-", ".", 1)  # e.g. "system.tag"
            # Handle multi-word: system-opchda → system.opchda (only first dash → dot)
            seen.add(slug)
            modules.append({"slug": slug, "qualified": qualified, "path": m.group(0)})

    return modules


def discover_functions(soup: BeautifulSoup, module_path: str) -> list[dict]:
    """
    Find all function links on a module index page.
    Returns list of {"name": "readBlocking", "qualified": "system.tag.readBlocking", "path": "..."}
    """
    functions = []
    seen = set()
    module_slug = module_path.rstrip("/").split("/")[-1]  # e.g. "system-tag"
    module_qualified = module_slug.replace("-", ".", 1)   # e.g. "system.tag"

    prefix = f"{module_path.rstrip('/')}/{module_slug}-"

    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].rstrip("/")
        if href.startswith(prefix) and href not in seen:
            seen.add(href)
            fn_name = href[len(prefix):]  # e.g. "readBlocking"
            qualified = f"{module_qualified}.{fn_name}"
            functions.append({
                "name": fn_name,
                "qualified": qualified,
                "path": href,
            })

    return functions


# ---------------------------------------------------------------------------
# Parallel workers
# ---------------------------------------------------------------------------

def _discover_module(mod: dict) -> tuple[dict, list] | None:
    """Fetch a module index page and return (mod, fn_links), or None on error."""
    try:
        soup = fetch(mod["path"])
        fn_links = discover_functions(soup, mod["path"])
        if not fn_links:
            log(f"  WARNING: no functions found for {mod['qualified']}, skipping")
            return None
        log(f"  {mod['qualified']}: {len(fn_links)} functions")
        return mod, fn_links
    except requests.HTTPError as e:
        log(f"  WARNING: {e} — skipping module {mod['qualified']}")
        return None


def _scrape_function(fn: dict) -> dict | None:
    """Fetch and parse one function page, or None on error."""
    try:
        soup = fetch(fn["path"])
        return parse_function_page(soup, fn["qualified"])
    except requests.HTTPError as e:
        log(f"  WARNING: {e} — skipping {fn['qualified']}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching module list...")
    root_soup = fetch(ROOT_PATH)
    modules = discover_modules(root_soup)

    if not modules:
        print("ERROR: No modules found. The page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(modules)} modules\n")

    # Phase 1: discover all module function lists in parallel
    print("Phase 1: discovering functions across all modules...")
    module_fn_links: dict[str, tuple[dict, list]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_discover_module, mod): mod for mod in modules}
        for future in as_completed(futures):
            result = future.result()
            if result:
                mod, fn_links = result
                module_fn_links[mod["slug"]] = (mod, fn_links)

    total_fns = sum(len(fns) for _, fns in module_fn_links.values())
    print(f"\nPhase 2: scraping {total_fns} function pages ({MAX_WORKERS} workers)...")

    # Phase 2: scrape all function pages in parallel
    # fn_results[slug][qualified_name] = parsed data dict
    fn_results: dict[str, dict[str, dict]] = {slug: {} for slug in module_fn_links}

    all_tasks = [
        (fn, slug)
        for slug, (_, fn_links) in module_fn_links.items()
        for fn in fn_links
    ]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_scrape_function, fn): (fn, slug) for fn, slug in all_tasks}
        done = 0
        for future in as_completed(futures):
            fn, slug = futures[future]
            data = future.result()
            if data:
                fn_results[slug][fn["qualified"]] = data
            done += 1
            if done % 50 == 0 or done == total_fns:
                log(f"  {done}/{total_fns} functions scraped")

    # Phase 3: render and write files (sequential; fast)
    print("\nPhase 3: writing help files...")
    index_modules = []
    for mod in modules:  # preserve original module order
        slug = mod["slug"]
        if slug not in module_fn_links:
            continue
        _, fn_links = module_fn_links[slug]
        short = slug.replace("system-", "", 1)
        qualified = mod["qualified"]

        fn_data_list = [
            fn_results[slug][fn["qualified"]]
            for fn in fn_links
            if fn["qualified"] in fn_results[slug]
        ]
        if not fn_data_list:
            continue

        out_path = OUT_DIR / f"ignition-{short}.txt"
        out_path.write_text(render_module_file(short, qualified, fn_data_list), encoding="utf-8")
        print(f"  Wrote {out_path.name}")
        index_modules.append({"slug": short, "qualified": qualified})

    (OUT_DIR / "ignition.txt").write_text(render_index(index_modules), encoding="utf-8")
    print("\nWrote doc/ignition.txt")
    print("Done. Run :helptags doc/ inside Neovim to generate the tags file.")


if __name__ == "__main__":
    main()
