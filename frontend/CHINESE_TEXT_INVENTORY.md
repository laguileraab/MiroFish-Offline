# Legacy i18n note (English-only project)

The MiroFish-Offline UI and developer docs target **English**. An older audit listed Chinese copy file-by-file; that list duplicated thousands of characters and drifted from the tree.

**If you need to find CJK text today**, search the repo (examples):

- VS Code / Cursor: search with regex (Unicode han) or literal Chinese phrases you care about.
- CLI (from repo root): `rg "[\p{Han}]" frontend/src` with a Unicode-aware ripgrep build, or search for known terms.

**Do not** treat this file as the source of truth for current strings—verify in `frontend/src` directly.
