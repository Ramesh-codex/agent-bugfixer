# CA order timestamps off by an hour in the dashboard

A merchant in California says the "local time placed" shown on one of their
orders is wrong. The order confirmation email (sent at the time of
purchase) says 3:30pm. Our ops dashboard, which converts the same stored
UTC timestamp to the customer's local time, shows 4:30pm for the exact same
order.

The UTC timestamp stored for the order hasn't changed between the two
places it's displayed, so whatever converts it to local time for the
dashboard seems to be using the wrong offset — but only for this merchant's
state.
