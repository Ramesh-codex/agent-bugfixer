# Checkout total is lower than subtotal + tax

Customers are seeing an order total at checkout that's lower than their
subtotal, even before any discount is applied. It looks like whatever tax
amount is calculated is being taken away from the subtotal instead of added
on top of it.

A customer buying a single $2.00 item in a state with sales tax should see
a total slightly above $2.00, not below it.
