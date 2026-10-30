# Last page of order history is unreachable when counts don't divide evenly

On the order history page, whenever the total number of orders isn't an
exact multiple of the page size, the trailing partial page of results is
never shown. The "next page" control disappears one page too early, and
there's no way to reach the last few orders through the UI.

Example: 11 orders with a page size of 5 should give 3 pages (5, 5, 1), but
the UI only ever offers 2 pages and the last order is unreachable.
