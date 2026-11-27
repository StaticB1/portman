# portman

A dev-server port manager for people who run more localhost servers than they can remember.

`portman` keeps one registry of `name → port → directory → start command`, starts and stops those
servers, tells you who is squatting on a port, and — the part that makes it worth installing —
audits its own registry against the file you consider authoritative, so a stale entry gets reported
instead of silently doing nothing.

It ships with a GTK tray and window for Linux desktops (see [The tray](#the-tray)).

```
$ portman list
  storefront
    ● web      :3012   ~/projects/storefront/web       npm run dev
    ○ ops      :8765   ~/projects/storefront/ops       npm run dev
  ○ blog       :3022   ~/projects/blog               npm run dev

$ portman doctor
  ✘ dead-dir      blog        directory no longer exists
                  fix: portman sync
  ⚠ port-drift    storefront:web  registry says 3012, inventory says 3112
                  fix: portman sync
```

## Why

Three symptoms this was written for:

- **A registry that lies.** Entries point at directories that were renamed or deleted a year ago.
  `start` prints a success banner, runs nothing and exits 0. `portman doctor` exists so that failure
  has to announce itself.
- **`kill 22` takes out sshd.** Port killers that trust you are one typo from ending your SSH
  session. System and database ports are refused without `--force`.
- **Three lists of ports and no source of truth.** A `.env`, a README table and a shell alias, all
  disagreeing. portman derives ports from one file and reports every place reality has drifted from
  it.

When it cannot tell whether a port is free — neither `ss` nor `lsof` available — it says `unknown`
and refuses to start, rather than assuming.

## Install

Requires Node.js ≥ 16. The tray additionally needs Python 3 with GTK 3, AppIndicator3 and Notify
(`python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-notify-0.7`, and either
`gir1.2-ayatanaappindicator3-0.1` or `gir1.2-appindicator3-0.1`).

```sh
git clone https://github.com/StaticB1/portman.git
cd portman
./install.sh
```

The installer copies into `~/.local/share/portman`, puts `portman` and `portman-tray` on
`~/.local/bin`, and registers the tray to start at login. It offers to `apt-get install` the GTK
packages if they are missing, and leaves an existing `~/.portman.json` alone.

`./uninstall.sh`, or `portman uninstall`, removes all of it.

The CLI works on its own on macOS and other Unixes — it needs `ss` or `lsof`. The tray is
Linux/GTK only.

## Use

```sh
portman add blog 3022 ~/code/blog -- npm run dev   # register a service
portman add storefront web 3012 ~/code/storefront/web  # register one inside a group
portman start blog                                 # foreground
portman start blog --detach                        # background → ~/.portman-logs/blog.log
portman logs blog -f                               # follow that log
portman open blog                                  # open it in your browser
portman ui                                         # the window: search, start, stop
portman start storefront                             # whole group, detached → ~/.portman-logs/
portman stop storefront
portman check 3012                                 # who is on this port
portman kill 3012                                  # end it (protected ports need --force)
portman status                                      # every listening port on the machine
portman port blog                                   # just the number, for scripts
```

`portman list --running` shows only what is up. `portman list --json` and `portman doctor --json`
are meant for scripting and CI.

Full command list: `portman help`.

## The tray

With thirty projects, a menu that lists all of them is a wall. So the tray menu shows only what you
would reach for: what is **running**, what you **pinned**, and what you **started last**. Each has
Open in browser, Start or Stop, Restart and View log.

Everything else is in the window (**Open portman…** in the menu, a middle-click on the icon, or
`portman ui`):

- **Search** by name, port or folder. Just start typing. Enter opens a running service in the
  browser, or starts a stopped one.
- Rows are sorted **Running**, **Pinned**, **Not running**, and a **Running** filter hides the rest.
- The **⋮** on a row has Restart, View log, Copy address, Open folder, Open terminal here, Pin,
  Edit and Remove.
- A start that fails says why, with the last lines the command printed and a **View log** button.
- A port held by some other program is shown as such, in amber, and portman asks before ending it.
- **Check the registry** (☰ menu) runs `portman doctor` and can load ports from the inventory file.

Servers started from the tray run with your shell's environment (so `nvm`, `pyenv` and friends
resolve), log to `~/.portman-logs/<name>.log`, and keep running when the tray quits.

## The inventory file

portman can treat a JSON file as the source of truth for ports and rebuild its registry from it.
That file is `FLEET.json`, found by looking at `$PORTMAN_SPINE`, then two directories up from
`portman.js`, then `~/projects`. Only `products[]` is read:

```json
{
  "products": [
    { "name": "blog",     "local": "blog",     "dev_port": 3022 },
    { "name": "storefront", "local": "storefront", "dev_port": 3012,
      "dev_services": { "web": { "port": 3012, "dir": "web" },
                        "ops": { "port": 8765, "dir": "ops" } } },
    { "name": "docs",     "local": "docs",     "dev_port": null }
  ]
}
```

- `local` is the product's path relative to the file's own directory. No `local`, no entry.
- `dev_port` is the port you would open in a browser. `null` means deliberately portless.
- `dev_services` describes a multi-service product: a bare port, or `{ port, dir }` where `dir` is
  relative to `local`.

Then:

```sh
portman sync --dry-run   # what would change
portman sync             # apply it; the inventory wins
portman doctor           # what still disagrees
```

`doctor` reports dead directories, duplicate ports, registry-vs-inventory drift, products the
registry never covered, registry entries the inventory has never heard of, and apps whose own `.env`
sets a different port from the one assigned to them — plus ports currently held by a process from
some other directory, which a `start` would have killed. Each finding names the command that fixes
it, and `doctor` exits non-zero when something must be fixed, so it drops straight into a health
check.

Registry entries that are deliberately outside the inventory are not findings once you say so:

```sh
portman scope scratch-api personal
```

Without an inventory file, every other command still works — you just manage the registry by hand.

## Start commands

`sync` infers one from what is in the directory: `artisan` → `php artisan serve`, a `package.json`
with a `dev` or `start` script → `npm run dev` / `npm start`, a `docker-compose.yml` →
`docker compose up`. It never guesses beyond that; anything else is reported by `doctor` as missing,
and you set it yourself:

```sh
portman set blog npm run dev
```

The assigned port is injected into the command where the framework expects it.

## Files

| Path | What |
| --- | --- |
| `~/.portman.json` | The registry. A corrupt one is refused, never silently reset. |
| `~/.portman.json.bak` | Previous good registry, written before each save. |
| `~/.portman-logs/` | Output from detached starts, from the CLI or the tray. |
| `~/.portman-tray.json` | The tray's pins and recently started list. |
| `~/.local/share/portman/` | Installed copy. |

## License

MIT — see [LICENSE](LICENSE).
