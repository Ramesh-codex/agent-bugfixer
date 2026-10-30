# Order total endpoint throws a 500 whenever a discount code is used

Orders with no discount code calculate and return a total just fine.

As soon as a customer applies any discount code to their cart, requesting
the order total results in a server error instead of a number. This is
happening for every discount code we tested, not just one specific code.

This is blocking checkout for any customer trying to use a promo.
