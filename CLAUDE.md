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

## Windows writes CRLF, and it will not fail until Linux runs it

**Python's `open()`/`write_text()` in text mode on Windows silently translates
`\n` into `\r\n`.** So does most of Windows tooling. Anything written or patched
here with Python and then executed on the Linux box breaks, and breaks in a way
that reads like ten unrelated bugs:

```
install.sh: line 4: $'\r': command not found
set: -: invalid option                  # from `set -e\r`
E: Invalid operation update             # from `apt update\r`
E: Unable to locate package git         # `git\r`, the LAST package on the line
./venv/bin/pip: cannot execute: required file not found
```

That last one is the tell worth memorising: the *final token on each line* eats
the `\r`, so the failure lands on whatever happened to be last, which is never
where the actual fault is.

`.gitattributes` now pins `* text=auto eol=lf`, so git normalises on commit and
on checkout. That is the durable fix. Two things it does not cover:

- A file written and executed **without** going through git.
- Your own verification. `bash -n script.sh` under **Git Bash tolerates CRLF**,
  so it printed "parses OK" on a script that could not run a single line on the
  target. This is the same failure as the spotifyd write-probe below: a check
  that passes in the wrong environment is not evidence.

When you write a shell script here, verify the bytes, not the syntax:

```bash
tr -cd '\r' < script.sh | wc -c     # must print 0
file script.sh                      # must NOT say "with CRLF line terminators"
```

And prefer `io.open(path, "w", newline="\n")` over `write_text()` for anything
Linux will execute.

## Verification that is unavailable here

- **No `node`, no Electron on the Windows box.** You cannot RUN the Electron
  shell there. You can still parse it: Chrome and Edge are both installed, and
  `new Function(src)` in a headless page parses without executing, so CommonJS
  `require` being undefined does not matter. That catches a botched splice and
  a syntax error both. A clean `systemctl --user restart cortana-dash` on the
  Linux box remains the only check of actual behaviour.
- **Page JS is fully runnable here, and that is worth using.** Anything under
  `Dashboard/package/` is browser code. Headless Chrome runs it, screenshots
  the real board, and dumps results out of the DOM:

  ```bash
  chrome.exe --headless=new --disable-gpu --no-sandbox \
    --virtual-time-budget=20000 --dump-dom "file:///.../harness.html" > dom.txt
  chrome.exe --headless=new --window-size=1280,820 \
    --screenshot=out.png "file:///.../Dusk%20Dashboard.dc.html"
  ```

  Two traps, both of which cost a round: a page copied outside `package/`
  needs its relative resource refs rewritten to absolute or the DC runtime
  never boots and you screenshot a raw `{{ template }}`; and test images must
  be **data: URIs**, because a `file://` image taints the canvas and
  `getImageData` throws. Python 3.12 + PIL are present too, which makes a
  reference implementation practical - `Dashboard/PALETTE.md` is the worked
  example.
- **Kotlin cannot be compiled here** - no gradle, no Android SDK. Structural
  checks (bracket balance, do the referenced members exist, is the XML
  well-formed) catch a bad splice; the real compiler is the `mobile-apk`
  workflow, which builds a signed APK on every push to main. Bump BOTH
  `versionCode` and `versionName` or it skips the release.
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
