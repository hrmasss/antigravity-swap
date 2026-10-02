# antigravity-swap

Multi-account switcher for the Antigravity CLI (`agy`). Switch between Google accounts without signing out, let `aswap` switch for you before an account runs out, see every account's quota in one table, and run long print-mode tasks that carry on to the next account when one hits its limit.

Modeled on [claude-swap](https://github.com/realiti4/claude-swap): same commands, same defaults where they make sense for agy.

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
uv tool upgrade antigravity-swap    # or: pipx upgrade antigravity-swap
```

For the binary, run the install line again.

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
   #  account                  5h   week  3p 5h  3p wk  5h resets  checked
*  1  you@example.com (home)   90%  44%   0%     0%     17:23      just now   limited until 17:13
   2  work@example.com (work)  0%   0%    0%     0%     17:23      just now
   3  side@example.com         0%   21%   0%     33%    17:23      just now
```

Numbers are **used** percent per window. `5h` and `week` are the Gemini models; `3p` is Claude and GPT through agy, which agy meters separately. `*` marks the account plain `agy` uses now. `aswap list --refresh` forces fresh readings; `--cached` skips the network.

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

Or let aswap choose: `aswap switch --strategy best` (most quota left), `--strategy next-available` (skip spent accounts), `--strategy consume-first` (weekly window that resets soonest).

After writing the new login, aswap runs `agy models` to prove it works. If the login was revoked, aswap switches back, quarantines that account, and tells you to re-add it.

**New agy launches use the new account. A session that is already running keeps the account it started with**, because agy holds its token in memory. Restart it to move it over; `agy -c` resumes the last conversation.

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

### Long tasks that survive a quota limit: `aswap exec`

Some agy limits never show in the quota numbers: an account can read 100% left and still stop mid-run with `RESOURCE_EXHAUSTED (code 429): Individual quota reached ... Resets in 4h42m2s`. `aswap exec` handles that case:

```bash
aswap exec -- -p "migrate the tests to pytest" --dangerously-skip-permissions
aswap exec work -- -p "..."                     # start on a specific account
aswap exec --strategy rotate --max-hops 3 -- -p "..."
```

Everything after `--` goes to agy. When the run stops on a quota error, aswap:

1. holds that account out until the reset time in the error (or `exec.limit_fallback_hours` if there is none),
2. picks the next account (`exec.strategy`, default `best`),
3. copies the conversation to that account and resumes it with `agy --conversation <id>` and a short "continue where you stopped" prompt (`exec.resume_prompt`).

A conversation is a local file, so the next account sees the whole history. Before each hop aswap checks the login with `agy models`; an account whose login is dead is quarantined and skipped instead of stalling on a sign-in prompt.

Output: with no `--output-format`, you get the final response as plain text. `--output-format json` gives agy's JSON; `stream-json` streams through. Exit code is agy's, or `3` when every account is spent.

On platforms where agy has one login per machine (see below), `exec` changes the default login as it hops, and says so.

### Run accounts side by side: `aswap run` (Linux)

```bash
aswap run 2                       # agy as account 2, in this terminal only
aswap run work -- -c              # everything after -- goes to agy
aswap run 2 --share-history       # share conversations with your default agy
```

Each account gets its own agy directory under aswap's data folder, with your settings, MCP config and skills mirrored from `~/.gemini` on every launch. When the session ends, the token agy renewed is saved back. Running the account that is already your default login just launches plain `agy`; `--require-session` refuses instead.

Bind a directory to an account and a bare `aswap run` there picks it:

```bash
aswap map 2 ~/work/client-app
cd ~/work/client-app && aswap run   # account 2
aswap map                           # list bindings
aswap unmap ~/work/client-app
```

## Platform support

Where agy keeps its login decides what is possible:

| | Linux (headless) | Windows | macOS, desktop Linux |
|---|---|---|---|
| Login lives in | `~/.gemini/antigravity-cli/antigravity-oauth-token` | Credential Manager, `gemini:antigravity` | system keyring |
| `add`, `list`, `switch`, `auto`, `exec` | yes | yes | experimental |
| `run` (two accounts at once) | yes | no | no |
| Quota for non-active accounts | always | until its token expires (~1h after last use) | until its token expires |

On Windows and keyring platforms the login is global: `agy --gemini_dir` changes where conversations go, not which account signs in. So one account is active per machine, and only the active one can renew its token. Force a backend with `ASWAP_BACKEND=file|wincred|keychain|secret-service`.

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

Bare `aswap` is `aswap list`.

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
| `exec.strategy` | `best` | `rotate`, `best`, `next-available`, `consume-first` |
| `exec.max_hops` | `0` | most account changes per run; `0` is every account once |
| `exec.resume_prompt` | (built in) | prompt sent when a conversation moves to the next account |
| `exec.limit_fallback_hours` | `5` | hold time when a quota error names no reset |
| `usage.max_age_seconds` | `300` | readings older than this are re-fetched |
| `usage.renew` | `true` | renew expired tokens with `agy models` before reading quota |
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

Rows may also carry `alias`, `disabled`, `limitedUntil` / `limitedReason`, `quarantined` / `quarantineReason`, and `usageError`.

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

## License

MIT
