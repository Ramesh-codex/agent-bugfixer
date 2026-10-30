# $50+ discount code not applying at exactly $50.00

Our promo banner says "Spend $50, get 10% off" and the code is supposed to
apply to any cart with a subtotal of $50 or more.

A customer wrote in saying the code `MIN50` didn't apply when their cart
subtotal was exactly $50.00. They added a $0.01 item to push it to $50.01
and the discount applied fine.

So it looks like the threshold is being treated as "more than $50" instead
of "$50 or more", which doesn't match what we advertise.
