# CA sales tax consistently a penny short

Finance flagged a discrepancy while reconciling CA orders: the sales tax we
charge is consistently one cent lower than what their reference tax
calculator produces, for certain order amounts.

It's not every order — most CA orders match finance's numbers exactly. It
only seems to happen on amounts where the tax works out to land right on a
rounding boundary (e.g. exactly half a cent).

Example: a $10.00 CA order should be taxed $0.73 according to finance's
table, but our system charged $0.72.
