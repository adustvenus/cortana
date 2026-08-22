# Working on Cortana

Repo-specific debugging discipline. This exists because the same class of
mistake has cost several build-and-test cycles more than once.

## Two machines, and you are probably not on the one that matters

A Windows dev box (edit, commit, push) and a Linux runtime box
(`Cortana-Core-v1`) where everything actually runs. There is **no SSH route
between them** in this repo - `secrets.sh` uses SSH only to reach GitHub.
Changes travel by push and pull, nothing else.

So a large class of bug is not reproducible where you are editing. When that
happens:

- Say so plainly instead of reasoning toward the most likely cause.
- Hand over one command that **discriminates between** hypotheses, not one
  that confirms the leading one. `curl -o file` versus `curl > file` settled
  several rounds of guessing about snap confinement in a single paste, because
  the only variable between them was which process called `open()`.
- Label anything you could not run as unverified, in the same breath as the
  claim - not in a footnote.

## Verification that is unavailable here

- **No `node`, no Electron on the Windows box.** JS under `Dashboard/app/`
  cannot be parsed there; `node --check` does not exist. Fall back to reading
  `git diff` and confirming the insertion count matches intent - that catches
  a botched splice, though not a syntax error. A clean
  `systemctl --user restart cortana-dash` on the Linux box is the real check.
- **`curl` on the Linux box is the snap build** (`/snap/bin/curl`), which has
  a private `/tmp`. `curl -o "$(mktemp -d)/f"` fails with exit 23 while the
  transfer itself succeeds. Never let curl open its own output file in a
  script here; redirect and let the unconfined shell do the `open()`.

## Failures that hide the next failure

The `Restart=always` units (`cortana-spotifyd`, `cortana-dash`) turn a config
error into a silent 5s respawn loop: `systemctl start` returns success and the
shell says nothing at all. Only the journal shows it, and only the **first**
error - each fix uncovers the next one down.

The spotifyd install took four passes for exactly this reason: a snap-curl
fetch failure, then a `cache_path` placeholder, then an ini-vs-TOML config,
then discovery. None was visible until the one before it was cleared.

Apply one fix, re-read the journal, repeat. Do not predict the second error -
looking is cheaper than being wrong, and being wrong costs a round trip to a
machine you cannot reach.

## A guard that passes may be measuring the wrong process

`install-spotifyd.sh` probed its temp directory for writability and checked
`df` for free space. Both passed while curl could not write there at all,
because the *shell* ran the probes and the shell was not confined. 200G free
and a successful write probe were both true and both irrelevant.

A precondition check is only evidence if it runs in the same context as the
thing that fails.
