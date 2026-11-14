# Autostart

`bin/fleet-up` is idempotent: it starts only the `autostart: true` sessions from `fleet.yaml`
that have no tmux session yet, plus the collector (tmux session `switchboard`). Run it from
whatever starts things at login. All options assume `scripts/install.sh` has linked
`fleet-up` into `~/.local/bin`.

## Linux / WSL with systemd (`systemd --user`)

```sh
mkdir -p ~/.config/systemd/user
cp autostart/claude-fleet.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-fleet.service
journalctl --user -u claude-fleet.service   # output; fleet-up also logs to $BOARD_HOME/fleet-up.log
```

In WSL this needs `systemd=true` under `[boot]` in `/etc/wsl.conf`. The user manager starts
when the distro boots, so you still need something to boot the distro at Windows logon —
see the next section.

## Windows (WSL) — scheduled task at logon

From PowerShell on the Windows side (no admin rights needed for a per-user logon task):

```powershell
powershell -ExecutionPolicy Bypass -File .\autostart\windows-task.ps1 -Distro Ubuntu
Start-ScheduledTask -TaskName claude-fleet      # test it now
.\autostart\windows-task.ps1 -Unregister        # remove it
```

Parameters: `-Distro` (from `wsl.exe -l -q`), `-TaskName` (default `claude-fleet`),
`-Visible` (show the console window), `-KeepAlive` (keep `wsl.exe` attached with
`sleep infinity`). Re-running the script replaces the task.

The window is hidden with `conhost.exe --headless`. If your Windows build does not support
that, use `-Visible`.

If the tmux sessions disappear shortly after the task finishes, WSL is shutting the distro
down once no client is attached. Either enable systemd in the distro (above) or register the
task with `-KeepAlive`.

## Checking

```sh
board doctor     # dependencies, hook, collector
board ls         # the fleet
tmux ls
```
