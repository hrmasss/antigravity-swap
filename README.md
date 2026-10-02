# antigravity-swap

Multi-account switcher for the Antigravity CLI (`agy`). Switch between Google accounts without signing out, let `aswap` switch for you before an account runs out, see every account's quota in one table, and run long print-mode tasks that carry on to the next account when one hits its limit.

Inspired by [claude-swap](https://github.com/realiti4/claude-swap) by Onur Cetinkol, which does the same for Claude Code. The command set, the defaults and the auto-switch design come from it; see [Credits](#credits).

## Installation

### Using uv (recommended)

```bash
uv tool install antigravity-swap
```

### Using pipx

```bash
pipx install antigravity-swap
```

### Standalone binary

No Python needed. Linux (x86_64, aarch64) and macOS (Apple silicon):

```bash
curl -fsSL https://raw.githubusercontent.com/hrmasss/antigravity-swap/main/install.sh | sh
```

Windows (PowerShell):

```powershell
$d = "$env:LOCALAPPDATA\Programs\aswap"; New-Item -ItemType Directory -Force $d | Out-Null
irm https://github.com/hrmasss/antigravity-swap/releases/latest/download/aswap-windows-x86_64.exe -OutFile "$d\aswap.exe"
# then add $d to your PATH
```

Every binary is listed with its checksum on the [releases page](https://github.com/hrmasss/antigravity-swap/releases).

### From source

```bash
git clone https://github.com/hrmasss/antigravity-swap.git
cd antigravity-swap
uv sync
uv run aswap --help
```

### Updating

```bash
aswap upgrade                       # detects uv, pipx or the binary installer
# or run your installer directly:
uv tool install --force --refresh antigravity-swap
pipx upgrade antigravity-swap
```

On Windows, `aswap upgrade` prints the command instead of running it, because Windows keeps `aswap.exe` locked while it runs.

## Usage

### Add your accounts

Sign in to agy with your first account, then:

```bash
aswap add
```

Sign in to agy with the next account (just run `agy` and pick it; no need to sign out first) and run `aswap add` again. Repeat for each account. Re-running `aswap add` on an account that is already stored updates its login instead of duplicating it.

Already keep accounts in separate agy directories (`agy --gemini_dir=...`)? Import them directly:

```bash
aswap add --from-dir ~/agy-accounts/work --alias work
```

### See every account

```bash
aswap list
```

```
Accounts:
  1: you@example.com [home] (active)
     ├ Gemini 5h:      23%   resets 13:42         in 4h 34m
     ├ Gemini 7d:       5%   resets Oct 8 18:23   in 6d 9h
     ├ Claude/GPT 5h:   0%   full
     └ Claude/GPT 7d:  33%   resets Oct 6 14:06   in 4d 4h
  2: work@example.com [work] (limited until 17:13, in 1h 2m)
     ├ Gemini 5h:       0%   full
     ├ Gemini 7d:      44%   resets Oct 5 13:02   in 3d 3h
     ├ Claude/GPT 5h:   0%   full
     └ Claude/GPT 7d:   0%   full

Running instances:
  ● TUI    ~/code/app      1: you@example.com [home]   (2 sessions)
  ● print  ~/code/infra    2: work@example.com [work]  (1 session)
```

Numbers are **used** percent per window, with when each window resets. agy meters two pools separately: the Gemini models, and Claude and GPT models through agy. `full` means nothing is used yet. `(active)` marks the account plain `agy` uses now. `aswap list --refresh` forces fresh readings, `--cached` skips the network, and `--table` prints one compact row per account.

**Running instances** lists every agy session on the machine, grouped by working directory, with the account it runs on: a session in an aswap directory by its slot, one started with `--gemini_dir` by the login in that directory, and one on the default login by whichever account was active when it started. A session that started before the first switch aswap recorded shows as "account unknown".

Readings come from the same endpoint the agy CLI uses. When an account's access token has expired, aswap renews it by running `agy models`, which lists models and spends no quota.

### Switch accounts

Rotate to the next account:

```bash
aswap switch
```

Or pick one:

```bash
aswap switch 2
aswap switch work@example.com
aswap switch work                 # by alias, once set with `aswap alias 2 work`
```

Or let aswap choose: `aswap switch --strategy most-quota`, `round-robin`, `next-available` or `soonest-reset` (see [Strategies](#strategies)).

After writing the new login, aswap runs `agy models` to prove it works. If the login was revoked, aswap switches back, quarantines that account, and tells you to re-add it.

**New agy launches use the new account. A session that is already running keeps the account it started with**, because agy holds its token in memory. Sessions started with `aswap run` move to the next account on their own when they run out.

### Automatic switching

```bash
aswap auto                        # foreground loop, checks every 60s
aswap auto --threshold 80         # switch earlier
aswap auto --pool 3p              # decide on the Claude/GPT quota instead of Gemini
aswap auto --once                 # one check, for cron or scripts
aswap auto --dry-run              # say what it would do, never switch
aswap auto --strategy consume-first
```

<details>
<summary>How it decides</summary>

- The **binding window** is the most-used window in the chosen pool (`gemini` by default, `3p`, or `all`). At or above `threshold` (default 90%), aswap looks for a better account.
- A proactive switch only lands on an account below the threshold **and** at least `hysteresis_pct` (default 10) points less used, so two accounts near the line never ping-pong.
- A cooldown (default 5 min) separates proactive switches. An account that is fully spent, or held out after a quota error, skips the cooldown.
- `consume-first` keeps you on the account whose weekly window resets soonest, switching even below the threshold, so quota that is about to reset is not wasted.
- Polling is bounded: the active account every tick, at most two others per tick, each about every ten minutes.
- When every account is over the threshold, it reports `all-exhausted` and checks less often until the first hold ends.
- Accounts that are disabled, quarantined or held out are never picked.

For cron, `--once` reports in its exit code: `0` switched, `1` error, `2` nothing to do, `3` blocked (no viable target). `--json` prints one JSON event per line:

```bash
*/5 * * * * aswap auto --once --json >> ~/.aswap-auto.log 2>&1
```

</details>

### Keep a session going when an account runs out: `aswap run`

Start agy through aswap instead of directly:

```bash
aswap run                          # the active account, in this terminal
aswap run work                     # start on a specific account
aswap run -- --model claude-sonnet-4-6 --dangerously-skip-permissions   # everything after -- goes to agy
```

When the account hits its quota mid-session, aswap:

1. holds that account out until the reset time agy reports (`Resets in 4h42m2s`), or `failover.limit_fallback_hours` if none is given,
2. picks the next account by `failover.strategy` (default `best`: the most quota left, read fresh at that moment),
3. stops agy, copies the conversation to the next account, and starts agy again with `--conversation <id> -i "Please continue where you left off."`.

The conversation is a local file, so the next account has the whole history and picks up the task. You see agy restart in the same terminal and carry on. If no other account is free, aswap leaves the session alone and tells you.

**Why a restart, and not a swap under the running session like claude-swap?** Claude Code re-reads its credentials file, so claude-swap can change account in place. agy keeps its login in memory for the life of the process; replacing the file under a running agy does nothing (tested). Restarting on the same conversation is the closest equivalent.

aswap watches agy's log for the final quota error. agy retries a quota error a couple of times first ("attempt 1 failed ... retrying"), and those retries never trigger a switch. Only the error that ends the turn does.

To make it the default, alias it:

```bash
alias agy='aswap run --'          # bash/zsh; agy -c becomes aswap run -- -c
```

`aswap run --no-failover` (or `aswap config set run.failover false`) runs a plain session on one account.

**Where each account runs.** On Linux each account other than the default login gets its own agy directory under aswap's data folder, with your settings, MCP config, skills and finished first-run setup mirrored from `~/.gemini` on every launch, so several accounts run side by side. On Windows and keyring platforms agy has one login per machine, so a hop switches the default login, and says so.

Bind a directory to an account and a bare `aswap run` there starts on it:

```bash
aswap map 2 ~/work/client-app
cd ~/work/client-app && aswap run   # account 2
aswap map                           # list bindings
aswap unmap ~/work/client-app
```

`--share-history` shares conversations between an account's directory and your default agy.

### Print-mode tasks: `aswap exec`

The same failover for `agy -p` runs, for scripts and unattended work:

```bash
aswap exec -- -p "migrate the tests to pytest" --dangerously-skip-permissions
aswap exec work -- -p "..."                     # start on a specific account
aswap exec --strategy round-robin --max-hops 3 -- -p "..."
```

Before each hop aswap checks the login with `agy models`; an account whose login is dead is quarantined and skipped instead of stalling on a sign-in prompt. Output: with no `--output-format`, you get the final response as plain text; `json` gives agy's JSON; `stream-json` streams through. Exit code is agy's, or `3` when every account is spent. `aswap run -- -p "..."` does the same thing.

### Strategies

One set of strategies everywhere a next account is picked (`switch --strategy`, `run`, `exec`, `auto`). Either name works:

| Strategy | Also called | Picks |
|---|---|---|
| `best` | `most-quota` | the account with the most quota left in the pool you are using. **Default** for `run`, `exec` and `auto`. |
| `rotate` | `round-robin` | the next account by number after the current one. Default for a bare `aswap switch`. |
| `next-available` | | the next account by number that still has quota |
| `consume-first` | `soonest-reset` | among accounts with room, the one whose weekly window resets soonest, so quota that is about to reset gets used first |

Accounts that are disabled, quarantined or held out after a quota error are never picked. The pool follows the model: `--model claude-*` or `gpt-*` judges the Claude/GPT quota, anything else the Gemini quota.

## Platform support

Where agy keeps its login decides what is possible:

| | Linux (headless) | Windows | macOS, desktop Linux |
|---|---|---|---|
| Login lives in | `~/.gemini/antigravity-cli/antigravity-oauth-token` | Credential Manager, `gemini:antigravity` | system keyring |
| `add`, `list`, `switch`, `auto`, `exec` | yes | yes | experimental |
| `run` with failover | yes | yes (switches the default login) | yes |
| two accounts at once | yes | no | no |
| Quota for non-active accounts | always | yes, by a brief login swap (below) | yes, by a brief login swap |

On Windows and keyring platforms the login is global: `agy --gemini_dir` changes where conversations go, not which account signs in. So one account is active per machine.

Access tokens last about an hour. To keep every account's quota fresh there, aswap renews an expired non-active account by putting its login in place for the few seconds `agy models` takes (no quota is spent), saving the renewed token, and putting your login back, all under aswap's lock. Running agy sessions are not affected, since they hold their token in memory. An agy you launch during those seconds would start on the borrowed account. Turn it off with `aswap config set usage.renew_inactive false`; non-active accounts then show `token expired` an hour after their last use. Force a backend with `ASWAP_BACKEND=file|wincred|keychain|secret-service`.

Tested against agy 1.2.x on Linux and Windows. macOS and desktop-keyring support follow how agy's keyring library stores the login and have not been tested on real machines yet; reports welcome.

## Other commands

```bash
aswap status                      # which account agy is using now
aswap refresh [N]                 # renew tokens and fetch quota now
aswap watch                       # live quota table
aswap disable 2 / aswap enable 2  # hold an account out of rotation (still a valid explicit target)
aswap limit 2 90m                 # hold it out for a while
aswap limit 2 --clear             # clear a hold or a quarantine
aswap alias 2 work                # short name, usable anywhere a number or email is
aswap alias 2 --unset
aswap remove 2                    # forget an account (agy's own login is untouched)
aswap config                      # settings, see below
aswap export backup.json          # all accounts to a file
aswap import backup.json          # skips accounts that exist; --force replaces
aswap purge                       # delete all aswap data
```

Bare `aswap` is `aswap list`. `rm` is short for `remove`.

### claude-swap flag spellings

The flag forms from claude-swap work too, so muscle memory carries over:

```bash
aswap --list              # aswap list
aswap --status            # aswap status
aswap --switch            # aswap switch
aswap --switch-to 2       # aswap switch 2
aswap --add-account       # aswap add
aswap --remove-account 2  # aswap remove 2
aswap --disable-account 2 / --enable-account 2
aswap --watch             # aswap watch (also --tui)
aswap --export f / --import f / --upgrade / --auto / --refresh
```

## Configuration

```bash
aswap config                               # every key, its value, and whether it is the default
aswap config set autoswitch.threshold 80   # validated
aswap config get autoswitch.threshold
aswap config unset autoswitch.threshold
aswap config path
```

| Key | Default | Meaning |
|---|---|---|
| `autoswitch.threshold` | `90` | used % that makes `auto` look for a better account |
| `autoswitch.interval_seconds` | `60` | seconds between checks |
| `autoswitch.cooldown_seconds` | `300` | minimum gap between proactive switches |
| `autoswitch.hysteresis_pct` | `10` | how much less used a target must be |
| `autoswitch.strategy` | `best` | `best` or `consume-first` |
| `autoswitch.pool` | `gemini` | `gemini`, `3p` or `all` |
| `switch.verify` | `true` | prove each new login with `agy models` |
| `run.failover` | `true` | `aswap run` restarts on the next account when one runs out |
| `failover.strategy` | `best` | how `run` and `exec` pick the next account (see Strategies) |
| `failover.max_hops` | `0` | most account changes per run; `0` is every account once |
| `failover.resume_prompt` | `Please continue where you left off.` | sent when a conversation moves to the next account |
| `failover.limit_fallback_hours` | `5` | hold time when a quota error names no reset |
| `usage.max_age_seconds` | `300` | readings older than this are re-fetched |
| `usage.renew` | `true` | renew expired tokens with `agy models` before reading quota |
| `usage.renew_inactive` | `true` | on single-login platforms, renew other accounts by a brief login swap |
| `agy.path` | (PATH) | the agy binary to use; the `ASWAP_AGY` environment variable overrides it |

## JSON output

`list`, `status`, `switch` and `config` take `--json`. Every payload has `schemaVersion: 1`; fields are only ever added.

```json
{
  "schemaVersion": 1,
  "activeAccountNumber": 1,
  "backend": "file",
  "accounts": [
    { "number": 1, "email": "you@example.com", "active": true, "usageStatus": "ok", "usageAgeSeconds": 42,
      "usage": { "gemini-5h": { "usedPct": 90.0, "resetsAt": "2026-10-01T17:23:31Z" },
                 "gemini-weekly": { "usedPct": 44.0, "resetsAt": "2026-10-05T07:02:44Z" } } }
  ]
}
```

Rows may also carry `alias`, `disabled`, `limitedUntil` / `limitedReason`, `quarantined` / `quarantineReason`, and `usageError`. `list` and `status` also carry `runningInstances`: `pid`, `mode` (`TUI` or `print`), `cwd`, `accountNumber`, `startedAt`.

## How it works

- `aswap add` copies agy's login (its OAuth token blob) into aswap's data folder, labelled by the email in its ID token.
- `aswap switch` saves the current login back to its slot first (agy may have renewed it), then writes the target's login where agy reads it, under a lock so two aswap processes never interleave.
- Which account is active is never stored: aswap reads agy's current login and matches it, so it cannot drift.
- Token renewal is agy's own: aswap runs `agy models`, which renews an expired access token and writes it back. aswap never holds an OAuth client secret.
- Quota comes from `loadCodeAssist` and `retrieveUserQuotaSummary` on Google's Cloud Code endpoint, called with each account's own access token.

## Data locations

| Platform | Folder |
|---|---|
| Linux | `${XDG_DATA_HOME:-~/.local/share}/antigravity-swap/` |
| macOS | `~/Library/Application Support/antigravity-swap/` |
| Windows | `%LOCALAPPDATA%\antigravity-swap\` |

Override with `ASWAP_HOME`. Inside: `accounts.json`, `credentials/<n>.json` (0600, folder 0700), `usage/`, `sessions/<n>/` (agy directories for `run` and `exec`), `settings.json`. `ASWAP_GEMINI_DIR` overrides agy's default `~/.gemini`.

The stored logins and `aswap export` files are live credentials in plaintext. Treat them like SSH keys.

## Uninstall

```bash
aswap purge
uv tool uninstall antigravity-swap    # or: pipx uninstall antigravity-swap
```

## Requirements

- The Antigravity CLI (`agy`), signed in
- Python 3.12+ (not needed for the standalone binary)

## Credits

antigravity-swap is an independent project for a different CLI, but its shape is borrowed from [claude-swap](https://github.com/realiti4/claude-swap) by Onur Cetinkol ([@realiti4](https://github.com/realiti4)), MIT licensed. Concepts taken from it:

- the commands and their spellings (`add`, `list`, `switch`, `run`, `auto`, `disable`, `alias`, `export`, and the `--list` / `--switch-to` flag forms)
- session mode: one account per terminal without touching the default login
- the auto-switch policy: a threshold on the binding window, hysteresis so accounts never ping-pong, a cooldown, and the `best` and `consume-first` strategies
- capturing a renewed token back before every switch, and quarantining logins that stop working
- the `list` layout with per-window usage, reset times and running instances, and the `--json` contract with `schemaVersion`

No code was copied; agy's login, quota and session model are different enough that everything is written for it. Thank you to the claude-swap authors.

## License

MIT
