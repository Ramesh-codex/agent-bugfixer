# Same-day order merging is behaving backwards

We merge orders from the same customer into one shipment when they were
placed on the same local business day. Since the last deploy this feature
is behaving backwards:

- Two orders placed a few hours apart on the same calendar day are now
  being treated as if they were placed on *different* days (they don't get
  merged anymore).
- Two orders that were clearly placed on two different calendar days are
  now getting merged together as if they happened on the same day.

Support has already had to manually split a few shipments that got merged
incorrectly.
