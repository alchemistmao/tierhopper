"""Smart progress: read "N of M" counters from a job's own output lines.

Jobs often print lines like "página 37 de 150", "page 37/150", "Epoch 3 of 10" or a tqdm bar.
We turn the latest one into {done, total, unit} so the dashboard can say "37 of 150 pages"
and estimate the time left from the recent pace, even when the script reports nothing else.
"""

from __future__ import annotations

import re

UNITS = {
    "página": "pages", "pagina": "pages", "páginas": "pages", "paginas": "pages", "page": "pages", "pages": "pages",
    "doc": "documents", "docs": "documents", "document": "documents", "documento": "documents",
    "documentos": "documents", "documents": "documents",
    "imagem": "images", "imagens": "images", "image": "images", "images": "images", "img": "images",
    "arquivo": "files", "arquivos": "files", "file": "files", "files": "files",
    "step": "steps", "steps": "steps", "passo": "steps", "passos": "steps",
    "epoch": "epochs", "epochs": "epochs", "época": "epochs", "epoca": "epochs",
    "batch": "batches", "batches": "batches", "lote": "batches", "lotes": "batches",
    "item": "items", "items": "items", "itens": "items", "sample": "samples", "samples": "samples",
    "amostra": "samples", "amostras": "samples", "it": "items",
}
# "<word> 37 de 150" / "37 of 150 <word>" / "37/150"; the unit word is optional.
COUNTER = re.compile(
    r"(?:(?P<before>[^\W\d_]+)\s+)?(?P<done>\d{1,7})\s*(?:/|de|of|out of)\s*(?P<total>\d{1,7})"
    r"(?:\s+(?P<after>[^\W\d_]+))?",
    re.IGNORECASE)
TQDM = re.compile(r"\|\s*(?P<done>\d{1,7})/(?P<total>\d{1,7})\s*\[")


def parse_counter(lines: list[str]) -> dict | None:
    """Latest counter with a named unit ("page 3 of 9"); else the latest generic one (tqdm, "3/90")."""
    generic = None
    for found in _counters(lines):
        if found["unit"] != "items":
            return found
        generic = generic or found
    return generic


def _counters(lines: list[str]):
    for line in reversed(lines):
        match = TQDM.search(line) or COUNTER.search(line)
        if not match:
            continue
        done, total = int(match["done"]), int(match["total"])
        if total < 2 or done > total:  # dates, versions, ratios like 16/9
            continue
        groups = match.groupdict()
        words = [(groups.get("before") or "").lower(), (groups.get("after") or "").lower()]
        unit = next((UNITS[w] for w in words if w in UNITS), "items")
        if unit == "items" and "/" in match.group(0) and not TQDM.search(line) and total < 20:
            continue  # bare small fractions (dates like 01/10, ratios) are usually not progress
        yield {"done": done, "total": total, "unit": unit}
