"""Generic page-slicing for list results."""

from dataclasses import dataclass
from typing import Any


@dataclass
class Page:
    items: list[Any]
    page_number: int
    page_size: int
    total_items: int

    @property
    def total_pages(self) -> int:
        if self.total_items == 0:
            return 0
        return (self.total_items + self.page_size - 1) // self.page_size

    @property
    def has_next(self) -> bool:
        return self.page_number < self.total_pages


def paginate(items: list[Any], page_number: int, page_size: int) -> Page:
    if page_number < 1:
        raise ValueError("page_number must be >= 1")
    if page_size < 1:
        raise ValueError("page_size must be >= 1")
    start = (page_number - 1) * page_size
    end = start + page_size
    return Page(
        items=items[start:end],
        page_number=page_number,
        page_size=page_size,
        total_items=len(items),
    )
