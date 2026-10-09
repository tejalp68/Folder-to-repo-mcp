# aiml-sync

**Turn any folder in your AIML course repo into its own GitHub repo, just by telling Claude.**

You create a project folder inside your course repo. You tell Claude: _"make this its own GitHub repo."_ Claude does it. Later, when you change that folder, you say _"sync"_ and the GitHub repo updates to match.

No more copy-pasting folders into new repos.

Your course repo is never touched. The tool keeps its own hidden copy in `~/.aiml-sync/` and pushes that copy to GitHub.

---

## Why I built this

One night I was finishing my AIML cohort assignment. The rule was simple: every project we make in the cohort must also be uploaded as its own separate GitHub repo.

So I was doing it the slow way. I copied everything from the project folder, pasted it into a new repo, and pushed it to GitHub. I did this again and again for every project.

Then I thought: **why don't I make an AI agent, an MCP server, that does this for me?**

The idea was simple. I would tell it something like: _"I made changes in my AIML repo, in the `mcp-assignment-2` folder. I want my separate repo to be exactly the same as that folder, and whenever I commit new changes there, update the separate repo too."_

It didn't work at the start. I kept changing things until it worked properly. Now I have an agent that does this boring job for me, and I don't have to copy-paste ever again.

If you are in the same situation, I hope it saves you the same time.

---

## What you need first

| Tool                  | What it's for                              | Check it works  |
| --------------------- | ------------------------------------------ | --------------- |
| **uv**                | Runs the tool and installs its packages    | `uv --version`  |
| **Git**               | Handles the files                          | `git --version` |
| **GitHub CLI (`gh`)** | Lets the tool create repos on your account | `gh --version`  |

Don't have uv? Install it, then close and reopen your terminal:

```
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Mac / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Setup (do this once)

**Step 1. Log in to GitHub**

```
gh auth login
```

**Step 2. Create the project and install the package**

```
uv init aiml-sync
cd aiml-sync
uv add "mcp[cli]"
```

Keep the quotes around `"mcp[cli]"`. Without them, your terminal may get confused by the square brackets.

**Step 3. Add the server file**

`uv init` creates a file called `main.py`. Delete it, and put `server.py` in the same folder.

**Step 4. Connect it to Claude**

Use the _full path_ to your `aiml-sync` folder in the commands below.

_If you use Claude Code:_

```
claude mcp add aiml-sync -- uv --directory /full/path/to/aiml-sync run server.py
```

_If you use Claude Desktop:_ open Settings, then Developer, then Edit Config. Add this, then restart the app.

```json
{
  "mcpServers": {
    "aiml-sync": {
      "command": "uv",
      "args": ["--directory", "/full/path/to/aiml-sync", "run", "server.py"]
    }
  }
}
```

On Windows, write the path with double backslashes, like `C:\\Users\\you\\aiml-sync`.

If Claude Desktop says it can't find `uv`, run `where uv` (Windows) or `which uv` (Mac/Linux). Then paste that full path in place of `"uv"` after `"command":`.

**Step 5. Test it**

Ask Claude: _"Which projects are being mirrored?"_

If it replies **"No projects registered yet,"** you're all set.

---

## How to use it

Just talk to Claude. Always give the full path to your project folder.

| You say                                                                    | What happens                                                 |
| -------------------------------------------------------------------------- | ------------------------------------------------------------ |
| "I created `~/AIML/week3-linear-regression`. Make it its own GitHub repo." | A new private repo is created and the folder is pushed to it |
| "Sync my projects."                                                        | Every project is updated on GitHub                           |
| "Sync week3-linear-regression."                                            | Only that project is updated                                 |
| "What would change if I synced week3-linear-regression?"                   | Shows a preview, pushes nothing                              |
| "Which projects are being mirrored?"                                       | Lists all your projects                                      |
| "Stop mirroring week3-linear-regression."                                  | Stops syncing it (the GitHub repo stays)                     |

New repos are **private** by default. Say _"make it public"_ if you want that.

---

## What if the GitHub repo already exists?

That's fine. You don't need to do anything special.

When you register a project, the tool checks whether a repo with that name is already on your GitHub:

- **No repo found:** it creates a new private one.
- **Repo found, but empty:** it fills it right away.
- **Repo found, with files in it** (for example, one you copy-pasted earlier): it shows you a **preview first** and pushes nothing yet.

The preview shows which files will be added, changed, or **deleted**. Watch for deletions. If your GitHub repo has files that are not in your folder (like a LICENSE or a README you wrote by hand), they will be deleted, because the repo becomes an exact copy of your folder.

If the preview looks right, tell Claude _"go ahead"_ and it updates the repo.

If the repo has a different name than your folder, just tell Claude, for example: _"link `~/AIML/week3` to my repo `my-old-repo`."_

Your repo's old history stays, and its branch name stays (`master` stays `master`). Your update is added on top as one new commit.

---

## What happens every time you sync

- The repo becomes an exact copy of your folder. New files are added, edited files are updated, and deleted files are removed.
- Anything in your `.gitignore` is left out (like `.venv` and `__pycache__`).
- A commit is made automatically and pushed.

---

## Safety checks

The tool **blocks the sync and pushes nothing** if it finds:

- Secret files like `.env`, `*.pem`, `*.key`, `id_rsa`, or `credentials.json`
- Text that looks like an API key (AWS, GitHub, OpenAI/Anthropic, Hugging Face, Google) or a private key, including inside notebooks

To fix it, remove the secret or add that file to the project's `.gitignore`, then sync again.

Files bigger than 95 MB are skipped, because GitHub doesn't accept anything over 100 MB. Claude will tell you which ones were skipped.

---

## Want it to sync by itself?

By default, syncing only happens when you ask Claude. To sync automatically after every commit in your course repo:

1. Open your course repo and create a file at `.git/hooks/post-commit`.
2. Put this inside it:

   ```sh
   #!/bin/sh
   uv --directory /full/path/to/aiml-sync run server.py --sync-all >/dev/null 2>&1 &
   ```

3. Run `chmod +x .git/hooks/post-commit`.

If it doesn't seem to run, use the full path to `uv` (from `which uv`) inside that file.

You can also run the same command by hand any time: `uv --directory /full/path/to/aiml-sync run server.py --sync-all`

---

## Good to know

- **Edit in your course folder, never on GitHub.** The next sync overwrites anything changed directly on GitHub. Old commits stay in the repo history, so nothing is lost for good.
- **Stopping a project doesn't delete anything.** The GitHub repo and the hidden copy both stay.
- **Where things are saved:** the project list and hidden copies live in `~/.aiml-sync/`.
- **History:** the GitHub repo only gets your files, not your course repo's commit history.
- **Folder name case:** on Mac and Linux, write your folder name exactly as it is (`AIML`, not `aiml`).
