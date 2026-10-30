from library.pagination import paginate


def test_first_page_has_full_page_size_items():
    page = paginate(list(range(1, 12)), page_number=1, page_size=5)
    assert page.items == [1, 2, 3, 4, 5]


def test_total_pages_rounds_up_for_partial_last_page():
    page = paginate(list(range(1, 12)), page_number=1, page_size=5)
    assert page.total_pages == 3


def test_last_partial_page_has_remaining_items():
    page = paginate(list(range(1, 12)), page_number=3, page_size=5)
    assert page.items == [11]


def test_last_page_has_no_next():
    page = paginate(list(range(1, 12)), page_number=3, page_size=5)
    assert page.has_next is False


def test_middle_page_has_next():
    page = paginate(list(range(1, 12)), page_number=2, page_size=5)
    assert page.has_next is True
