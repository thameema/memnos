# Releasing memnos

Publishing to PyPI is automated by `.github/workflows/release.yml`. You never run `twine` by
hand and **no PyPI token is stored anywhere** — GitHub Actions authenticates to PyPI with
short-lived OIDC credentials via **Trusted Publishing**.

## One-time setup (per PyPI project)

Do this once for **`memnos`** and once for **`memnos-sdk`**:

1. Sign in to PyPI → open the project → **Manage → Publishing**.
2. Under **"Add a new trusted publisher" → GitHub**, enter:
   - **Owner:** `thameema`
   - **Repository:** `memnos`
   - **Workflow name:** `release.yml`
   - **Environment:** *(leave blank)*
3. Save. (After this, you can delete the manual PyPI API token — it's no longer needed.)

## Cutting a release

The version number is the single source of truth; bump it, tag it, release it:

1. Bump the version:
   - `pyproject.toml` → `version = "0.1.2"` (the `memnos` package)
   - `sdk/pyproject.toml` → bump too **only if the SDK changed** (`skip-existing` ignores it otherwise)
2. Commit + push to `master`.
3. Create the GitHub release (this is what triggers publishing):
   ```bash
   git tag v0.1.2 && git push origin v0.1.2
   gh release create v0.1.2 --target master --title "memnos v0.1.2 — <summary>" --generate-notes
   ```
   (or click **Releases → Draft a new release** in the GitHub UI.)
4. The **Release to PyPI** workflow builds both packages and publishes them. Watch it under
   the repo's **Actions** tab.

That's it — `pip install -U memnos` / `uv tool upgrade memnos` picks up the new version once
the workflow goes green. Keep `memnos` + `memnos-sdk` + the git tag on the same version line
(stay in **0.x** until adoption).

## Why not publish on every push to `master`?

That would require auto-incrementing versions and would spam PyPI with releases nobody asked
for. Tag/release-triggered publishing keeps a human in control of *when* a version ships,
while CI handles *how*. CI on every push/PR (`.github/workflows/ci.yml`) still guards
correctness — only the **publish** is gated behind a release.
