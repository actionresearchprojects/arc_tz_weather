# Updating omnisense_w_p_s

## Full update workflow

### 1. Organise the data files (local only — data/ is gitignored)
- Rename the new download to `omnisense_DDMMYY.csv` (e.g. `omnisense_270226.csv`)
- Create `data/legacy/` if it doesn't exist yet
- Move the current CSV out of `data/` into `data/legacy/`
- Put the new `omnisense_DDMMYY.csv` into `data/`

```
data/
  omnisense_270226.csv       ← new file goes here
  legacy/
    omnisense_070226.csv     ← old file moved here
```

### 2. Rebuild the dashboard
```bash
cd '/Users/archwrth/Downloads/ipynb_graphs/omnisense_w_p_s'
python build.py
```

### 3. Push to GitHub

> `data/` is gitignored — the CSV moves above are invisible to git. Only `index.html` needs pushing.

```bash
git add index.html && git commit -m "update data" && git push
```

---

## Push code changes (build.py edits etc.)
```bash
git add build.py CLAUDE.md && git commit -m "describe your change" && git push
```
