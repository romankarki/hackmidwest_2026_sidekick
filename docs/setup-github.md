# Connect GitHub to Sidekick

**Time:** ~10 minutes · **Cost:** free

Sidekick uses GitHub to read deploy history and diffs, create branches and open pull requests on **one repo** — your ShopLite repo. You'll create a token that can only touch that repo.

---

## Step 1 — Put ShopLite on GitHub (skip if it's already there)

You need the [GitHub CLI](https://cli.github.com/) (`gh`) or the GitHub website.

```bash
cd shoplite
git init
git add -A
git commit -m "ShopLite"
gh auth login                     # once, if you haven't used gh before
gh repo create shoplite --private --source=. --push
```

✅ **Check:** `https://github.com/<your-username>/shoplite` shows your files.

---

## Step 2 — Create a fine-grained personal access token (PAT)

1. GitHub → click your avatar (top right) → **Settings**.
2. Left sidebar, at the bottom → **Developer settings**.
3. **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
4. Fill in:
   | Field | Value |
   |---|---|
   | Token name | `sidekick-hackathon` |
   | Expiration | **7 days** (or 30) — short is safer |
   | Repository access | **Only select repositories** → pick `shoplite` |
5. Under **Permissions → Repository permissions**, set:
   | Permission | Access |
   |---|---|
   | Contents | **Read and write** (branches, commits) |
   | Pull requests | **Read and write** |
   | Metadata | Read-only (selected automatically) |
   Leave everything else at *No access*.
6. **Generate token** → copy it now (starts with `github_pat_…`). GitHub shows it only once.

> 🔒 Treat the token like a password. Don't paste it into chats, commits or screenshots.

---

## Step 3 — Add it to Sidekick

1. Open the dashboard → **Integrations** → **GitHub** → **Connect**.
2. Fill in:
   - **Personal access token:** the `github_pat_…` value
   - **Repository:** `your-username/shoplite`
   - **Base branch:** `main`
3. Click **Save & test**.

✅ **Check:** the card turns **Connected** and says `Signed in as <you> · your-username/shoplite (push access)`.

| If you see | Fix |
|---|---|
| `HTTP 401: Bad credentials` | Token copied wrong or expired — generate a new one |
| `repo …: HTTP 404 Not Found` | Repository name typo, or the token wasn't given access to that repo (Step 2.4) |
| `(read-only access)` | Contents permission is Read-only — edit the token, set **Read and write** |

---

## Spending guardrails

GitHub repos and PATs are free. The only thing that can cost money is **GitHub Actions minutes** on private repos above the free allowance.

1. GitHub → **Settings** → **Billing and licensing** → **Budgets and alerts**.
2. Make sure the **Actions** budget is **$0** with **"Stop usage when budget limit is reached"** on (this is the default for personal accounts — just confirm it).

That's it — with a $0 budget, Actions simply stops when the free minutes run out instead of charging you.

---

## When the hackathon is over
- GitHub → Settings → Developer settings → Fine-grained tokens → **Delete** `sidekick-hackathon`.
- Dashboard → Integrations → GitHub → **Disconnect** (deletes the saved token).
