# Catalog pagination is losing products

We paginate the product catalog with a fixed page size. Several merchants
have reported that their product counts don't add up: items that definitely
exist in the catalog never show up on *any* results page, no matter how many
pages you click through.

It's not random — for a given page size, it's always the same items that
go missing, and the gap repeats at a regular interval as you page through
the full catalog.

Reproduction:
1. Seed a catalog with 11 known SKUs.
2. Page through all results with page size 5.
3. Count the total number of distinct SKUs actually returned across all
   pages. It's less than 11.
