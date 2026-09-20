# Permission self-heal (bankai-harden)

BankAI's runtime tree (`/opt/bankai`) keeps reverting to world-writable `0777`
because deploys copy the source from a `/mnt/c` (Windows/DrvFs) worktree, where
every file is `0777`, and preserve those modes. This restores owner(root)-only,
which is safe because both BankAI services run as root and nothing non-root needs
the tree. See `bankai/security/CHARTER.md` ("the one sanctioned auto-action").

Files here:
- `bankai-harden.sh` — the self-heal. Idempotent + cheap: only re-chmods paths
  that have actually drifted, and records each heal to the tamper-evident ledger
  (`perms_relocked`). Always exits 0 so it can gate service start.
- `bankai-harden.service` / `bankai-harden.timer` — run it every 5 min + on boot.

## Install (as root, on the box)

    cp bankai/scripts/bankai-harden.sh /root/bankai-harden.sh   # OUTSIDE /opt so a deploy can't overwrite it
    chown root:root /root/bankai-harden.sh && chmod 700 /root/bankai-harden.sh
    cp bankai/scripts/bankai-harden.service /etc/systemd/system/
    cp bankai/scripts/bankai-harden.timer   /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now bankai-harden.timer

Also add `ExecStartPre=-/root/bankai-harden.sh` to `bankai.service` so a restart
always starts locked.

## The real fix (still worth doing)

This is a backstop. The deploy itself should stop re-loosening perms: rsync with
`--chmod=Dgo=,Fgo=` (or a post-deploy `chown -R root:root /opt/bankai && chmod -R
go-rwx /opt/bankai && chmod 600 /opt/bankai/.env`). Verify:

    stat -c '%a %n' /opt/bankai /opt/bankai/bankai
