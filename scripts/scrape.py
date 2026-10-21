#!/usr/bin/env python3
"""
Scrape the Ignition 8.3 scripting function docs and write them as vim help files.

How it works:
    1. Fetch the scripting-functions index page and collect the module links
       (system.tag, system.db, ...).
    2. Fetch each module page and collect its function links.
    3. Fetch and parse every function page into a `Function`.
    4. Render one help file per module (doc/ignition-<module>.txt) plus the
       index (doc/ignition.txt).

If the docs site changes its markup, the places to look are
`discover_modules`, `discover_functions` and `parse_function_page`.

Usage:
    pip install requests beautifulsoup4 lxml
    python scripts/scrape.py [--out DIR] [--workers N] [--verbose]
    nvim --headless -c "helptags doc/" -c "quit"
"""

import argparse
import re
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://docs.inductiveautomation.com"
ROOT_PATH = "/docs/8.3/appendix/scripting-functions"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "doc"
DEFAULT_WORKERS = 10
REQUEST_TIMEOUT = 30  # seconds

# Vim help files are 78 columns wide (see the modeline written at the bottom).
WIDTH = 78
SEPARATOR = "-" * WIDTH
MODELINE = "vim:tw=78:ft=help:norl:"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Param:
    name: str
    type: str  # empty when the docs table has no type column
    description: str


@dataclass
class Syntax:
    """One call signature. Overloaded functions have several."""

    signature: str = ""
    params: list[Param] = field(default_factory=list)
    returns: str = ""


@dataclass
class Function:
    name: str  # fully qualified, e.g. "system.tag.readBlocking"
    description: str = ""
    syntaxes: list[Syntax] = field(default_factory=list)
    scope: str = ""
    examples: list[str] = field(default_factory=list)


@dataclass
class FunctionLink:
    name: str  # fully qualified, e.g. "system.tag.readBlocking"
    path: str  # URL path of the function's page


@dataclass
class Module:
    slug: str  # URL slug, e.g. "system-tag"
    path: str  # URL path of the module's page
    functions: list[FunctionLink] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Fully qualified name, e.g. "system.tag"."""
        return self.slug.replace("-", ".", 1)

    @property
    def short_name(self) -> str:
        """Name without the "system." prefix, e.g. "tag"."""
        return self.slug.removeprefix("system-")


# ---------------------------------------------------------------------------
# Logging and HTTP
# ---------------------------------------------------------------------------

_print_lock = threading.Lock()
_thread_local = threading.local()
_verbose = False


def log(msg: str) -> None:
    """Thread-safe print."""
    with _print_lock:
        print(msg)


def get_session() -> requests.Session:
    """Return this thread's HTTP session (sessions aren't safe to share)."""
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        session.headers["User-Agent"] = "ignition-docs.nvim scraper/1.0"
        _thread_local.session = session
    return _thread_local.session


def fetch(path: str) -> BeautifulSoup:
    url = BASE_URL + path
    if _verbose:
        log(f"  GET {url}")
    resp = get_session().get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    # The site serves minified HTML that omits optional end tags (</td>, </tr>);
    # html.parser doesn't infer them and nests cells, so use lxml.
    return BeautifulSoup(resp.text, "lxml")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def text_of(element: Tag | None) -> str:
    """Element text with all whitespace runs collapsed to single spaces."""
    if element is None:
        return ""
    text = element.get_text(" ", strip=True).replace("​", "")  # zero-width space
    return " ".join(text.split())


def wrap(text: str, indent: int | str = 2) -> list[str]:
    """
    Word-wrap text to the help file width.

    `indent` is a number of spaces or a literal prefix for the first line;
    continuation lines are indented to the same width.
    """
    first = " " * indent if isinstance(indent, int) else indent
    return textwrap.wrap(
        text,
        # One column short of WIDTH, matching the existing generated docs.
        width=WIDTH - 1,
        initial_indent=first,
        subsequent_indent=" " * len(first),
        break_long_words=False,
        break_on_hyphens=False,
    )


def right_align(left: str, right: str) -> str:
    """Pad between left and right so that right ends at column WIDTH."""
    return left + " " * max(1, WIDTH - len(left) - len(right)) + right


def help_tag(name: str) -> str:
    """A tag definition: `:help name` jumps here."""
    return f"*{name}*"


def help_link(name: str) -> str:
    """A link to a tag."""
    return f"|{name}|"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

MODULE_HREF = re.compile(rf"^{re.escape(ROOT_PATH)}/(system-\w+)$")


def normalize_href(href: str) -> str:
    return href.split("?")[0].rstrip("/")


def discover_modules(soup: BeautifulSoup) -> list[Module]:
    """Find the module links on the scripting-functions index page, in page order."""
    modules: dict[str, Module] = {}
    for a in soup.find_all("a", href=True):
        href = normalize_href(a["href"])
        match = MODULE_HREF.match(href)
        if match and match.group(1) not in modules:
            modules[match.group(1)] = Module(slug=match.group(1), path=href)
    return list(modules.values())


def discover_functions(soup: BeautifulSoup, module: Module) -> list[FunctionLink]:
    """
    Find the function links on a module page.

    Function pages live under the module page and repeat its slug, e.g.
    .../system-tag/system-tag-readBlocking for system.tag.readBlocking.
    """
    prefix = f"{module.path}/{module.slug}-"
    functions: dict[str, FunctionLink] = {}
    for a in soup.find_all("a", href=True):
        href = normalize_href(a["href"])
        if href.startswith(prefix) and href not in functions:
            name = f"{module.name}.{href.removeprefix(prefix)}"
            functions[href] = FunctionLink(name=name, path=href)
    return list(functions.values())


# ---------------------------------------------------------------------------
# Function page parsing
# ---------------------------------------------------------------------------

# Heading keyword -> section. Checked in order and the first match wins, so a
# heading like "Syntax - Parameters" counts as a syntax section.
SECTION_KEYWORDS = [
    ("description", "description"),
    ("syntax", "syntax"),
    ("parameter", "params"),
    ("return", "returns"),
    ("scope", "scope"),
    ("example", "examples"),
]


def section_for_heading(heading: str) -> str | None:
    heading = heading.lower()
    for keyword, section in SECTION_KEYWORDS:
        if keyword in heading:
            return section
    return None


def parse_params_table(table: Tag) -> list[Param]:
    """Parse a parameters table: (type, name, description) or (name, description)."""
    params = []
    for row in table.find_all("tr")[1:]:  # skip header row
        cells = [text_of(cell) for cell in row.find_all(["td", "th"])]
        if len(cells) >= 3:
            params.append(Param(type=cells[0], name=cells[1], description=cells[2]))
        elif len(cells) == 2:
            params.append(Param(type="", name=cells[0], description=cells[1]))
    return params


def parse_code_block(pre: Tag) -> str:
    """Extract source code from a <pre>, preserving line breaks."""
    # The syntax highlighter puts each line in its own <span class="token-line">.
    lines = pre.find_all("span", class_="token-line")
    if lines:
        return "\n".join(line.get_text().rstrip() for line in lines)
    for br in pre.find_all("br"):
        br.replace_with("\n")
    return pre.get_text()


def parse_function_page(soup: BeautifulSoup, name: str) -> Function:
    """
    Extract a function's docs from its page.

    Walks the page's headings and content elements in document order. Each
    heading switches the current section (see SECTION_KEYWORDS); content is
    then attributed to that section. Each "Syntax" heading starts a new
    `Syntax`, so the parameters and returns that follow belong to it.
    Unrecognized headings (e.g. "Client Permission Restrictions") leave the
    current section unchanged.
    """
    function = Function(name=name)
    main = soup.find("article") or soup.find("main") or soup.body
    if main is None:
        return function

    section = None
    syntax = None  # the Syntax currently being filled in

    for element in main.find_all(["h1", "h2", "h3", "h4", "table", "pre", "p", "code"]):
        tag = element.name

        if tag in ("h1", "h2", "h3", "h4"):
            new_section = section_for_heading(text_of(element))
            if new_section == "syntax":
                syntax = Syntax()
                function.syntaxes.append(syntax)
            if new_section:
                section = new_section

        elif tag == "code" and section == "syntax" and syntax is not None:
            # The signature is the first <code> that looks like a call; other
            # <code> elements in this section are inline parameter names.
            text = text_of(element)
            if not syntax.signature and "system." in text and "(" in text:
                syntax.signature = text

        elif tag == "table" and section == "params" and syntax is not None:
            syntax.params.extend(parse_params_table(element))

        elif tag == "pre" and section == "examples":
            function.examples.append(parse_code_block(element))

        elif tag == "p":
            # Only the first paragraph of each section is kept.
            text = text_of(element)
            if not text:
                continue
            if section == "description" and not function.description:
                function.description = text
            elif section == "scope" and not function.scope:
                function.scope = text
            elif section == "returns" and syntax is not None and not syntax.returns:
                syntax.returns = text

    return function


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def tag_name(qualified_name: str) -> str:
    """Help tag for a module or function, e.g. "ignition-system.tag"."""
    return f"ignition-{qualified_name}"


def render_params(params: list[Param]) -> list[str]:
    """Render params as aligned columns: {name}  (type)  description."""
    name_width = max(len(p.name) for p in params) + 2  # +2 for the braces
    type_width = max(len(p.type) for p in params) + 2  # +2 for the parens
    lines = []
    for p in params:
        name_col = f"{{{p.name}}}".ljust(name_width)
        type_col = (f"({p.type})" if p.type else "").ljust(type_width)
        prefix = f"    {name_col}  {type_col}  "
        lines.extend(wrap(p.description, indent=prefix) or [prefix.rstrip()])
    return lines


def render_function(function: Function) -> list[str]:
    lines = [
        SEPARATOR,
        right_align(f"{function.name}()", help_tag(tag_name(function.name))),
        "",
    ]

    if function.description:
        lines += [*wrap(function.description), ""]

    for syntax in function.syntaxes:
        if syntax.signature:
            # ">" ... "<" marks a code block in vim help.
            lines += ["  Syntax: >", f"    {syntax.signature}", "<", ""]
        if syntax.params:
            # A trailing "~" highlights the line as a header.
            lines += ["  Parameters: ~", *render_params(syntax.params), ""]
        if syntax.returns:
            lines += ["  Returns: ~", *wrap(syntax.returns, indent=4), ""]

    if function.scope:
        lines += [f"  Scope: {function.scope}", ""]

    if function.examples:
        # Only the first example; the rest are usually variations on it.
        example = [f"    {line}" for line in function.examples[0].splitlines()]
        lines += ["  Example: >", *example, "<", ""]

    return lines


def render_module_file(module: Module, functions: list[Function]) -> str:
    """Render doc/ignition-<module>.txt."""
    title = module.name.upper()
    lines = [
        right_align(
            help_tag(f"ignition-{module.short_name}.txt"),
            f"Ignition {module.name} functions",
        ),
        "",
        right_align(title, help_tag(tag_name(module.name))),
        "",
        *(f"  {help_link(tag_name(fn.name))}" for fn in functions),
        "",
    ]
    for function in functions:
        lines += render_function(function)
    lines += ["", MODELINE]
    return "\n".join(lines)


def render_index(modules: list[Module]) -> str:
    """Render doc/ignition.txt, the entry point for `:help ignition`."""
    intro = (
        "Vim help reference for Ignition 8.3 system scripting functions. "
        f"Generated from {BASE_URL}{ROOT_PATH}"
    )
    lines = [
        right_align(help_tag("ignition.txt"), "Ignition 8.3 Scripting Functions"),
        "",
        right_align("IGNITION SCRIPTING FUNCTIONS", help_tag("ignition")),
        "",
        *wrap(intro, indent=0),
        "",
        right_align("MODULES", help_tag("ignition-modules")),
        "",
        *(f"  {help_link(tag_name(m.name)):<45}  {m.name}" for m in modules),
        "",
        MODELINE,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def load_module_functions(module: Module) -> Module | None:
    """Fill in module.functions from its page. Returns None if it can't be used."""
    try:
        module.functions = discover_functions(fetch(module.path), module)
    except requests.RequestException as e:
        log(f"  WARNING: {e} - skipping module {module.name}")
        return None
    if not module.functions:
        log(f"  WARNING: no functions found for {module.name}, skipping")
        return None
    log(f"  {module.name}: {len(module.functions)} functions")
    return module


def scrape_function(link: FunctionLink) -> Function | None:
    try:
        return parse_function_page(fetch(link.path), link.name)
    except requests.RequestException as e:
        log(f"  WARNING: {e} - skipping {link.name}")
        return None


def scrape_all_functions(
    pool: ThreadPoolExecutor, links: list[FunctionLink]
) -> dict[str, Function]:
    """Scrape every function page, keyed by qualified name. Failures are omitted."""
    results = {}
    for done, function in enumerate(pool.map(scrape_function, links), start=1):
        if function:
            results[function.name] = function
        if done % 50 == 0 or done == len(links):
            log(f"  {done}/{len(links)} functions scraped")
    return results


def write_help_files(
    out_dir: Path, modules: list[Module], functions: dict[str, Function]
) -> None:
    index_modules = []
    for module in modules:
        module_functions = [
            functions[link.name] for link in module.functions if link.name in functions
        ]
        if not module_functions:
            continue
        path = out_dir / f"ignition-{module.short_name}.txt"
        path.write_text(render_module_file(module, module_functions), encoding="utf-8")
        log(f"  Wrote {path.name}")
        index_modules.append(module)

    index_path = out_dir / "ignition.txt"
    index_path.write_text(render_index(index_modules), encoding="utf-8")
    log(f"  Wrote {index_path.name}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"output directory (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"concurrent requests (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log every request"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    global _verbose
    args = parse_args(argv)
    _verbose = args.verbose
    args.out.mkdir(parents=True, exist_ok=True)

    log("Fetching module list...")
    modules = discover_modules(fetch(ROOT_PATH))
    if not modules:
        sys.exit("ERROR: No modules found. The page structure may have changed.")
    log(f"Found {len(modules)} modules\n")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        log("Discovering functions in each module...")
        modules = [m for m in pool.map(load_module_functions, modules) if m]

        links = [link for module in modules for link in module.functions]
        log(f"\nScraping {len(links)} function pages ({args.workers} workers)...")
        functions = scrape_all_functions(pool, links)

    log(f"\nWriting help files to {args.out}...")
    write_help_files(args.out, modules, functions)
    log("\nDone. Run :helptags doc/ inside Neovim to generate the tags file.")


if __name__ == "__main__":
    main()
