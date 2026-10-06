"""Stdlib-only Serbian public holidays (Zakon o državnim i drugim praznicima u RS, čl. 1, 1a, 2, 3a).
Non-working days for everyone (state + Orthodox religious days from čl. 2). Sunday rule (čl. 3a) applies to
state holidays: if one of its dates falls on Sunday, the first following working day is off."""
import datetime as dt
def orthodox_easter(y):
    a, b, c = y % 4, y % 7, y % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    return dt.date(y, month, day + 1) + dt.timedelta(days=13)  # Julian -> Gregorian (1900-2099)
def rs_holidays(y):
    D = dt.date; h = {}
    state = [("Nova godina", [D(y,1,1), D(y,1,2)]), ("Sretenje - Dan državnosti", [D(y,2,15), D(y,2,16)]),
             ("Praznik rada", [D(y,5,1), D(y,5,2)]), ("Dan primirja", [D(y,11,11)])]
    e = orthodox_easter(y)
    relig = [("Božić", [D(y,1,7)]), ("Vaskrs", [e - dt.timedelta(2), e - dt.timedelta(1), e, e + dt.timedelta(1)])]
    for n, ds in state + relig:
        for d in ds: h.setdefault(d, []).append(n)
    for n, ds in state:              # čl. 3a: Sunday -> first next working day
        if any(d.weekday() == 6 for d in ds):
            x = max(ds) + dt.timedelta(1)
            while x.weekday() >= 5 or x in h: x += dt.timedelta(1)
            h.setdefault(x, []).append(n + " (prenet neradni dan)")
    return dict(sorted(h.items()))
if __name__ == "__main__":
    for y in (2026, 2027):
        for d, n in rs_holidays(y).items(): print(d, d.strftime("%a"), "; ".join(n))
