# History templates

Four files that tell the engine what your manual payroll already paid and took
back, so nothing is paid twice at go-live. Each file here has the header row and
one made-up example row.

Copy them to `data/history/` (that folder is never put in git), delete the example
rows, and fill them from your own payroll records. Then:

```
python scripts/load_history.py            # checks the files and prints each rep's totals
python scripts/load_history.py --write    # loads them
```

What each column means is at the top of `scripts/load_history.py`, and the whole
go-live procedure is in `ONBOARDING.md`.
