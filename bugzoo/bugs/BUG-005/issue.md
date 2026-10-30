# Discount receipt shows codes from a different order

A few customers have reported that the printed discount summary on their
order receipt lists promo codes that have nothing to do with their
purchase. One customer with no discount at all got a receipt listing a
`SAVE10` line that we traced back to a completely unrelated order placed
earlier that day.

It seems to get worse the longer the server has been running — restarting
the service seems to temporarily "fix" it, which is a weird clue.
