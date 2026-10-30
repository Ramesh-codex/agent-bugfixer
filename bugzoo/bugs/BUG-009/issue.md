# Reusable discount code only works for the first order of the day

We run a multi-use promo code (`SAVE10`) during flash sales — it's meant to
apply to every qualifying order, not just one.

Today the first order that used `SAVE10` got the 10% off correctly. Every
order after that, for the rest of the sale, did not get the discount at
all, even though they clearly qualified (same subtotal, same code). The
code itself wasn't deactivated in the admin panel.

Restarting the order service made it work again for exactly one more order
before it stopped applying again.
