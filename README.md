# EMEC-AMS Pi

The app on each machine's Raspberry Pi. It reads cards, runs the relay and talks to the dashboard's database.

## Imaging the SD card

A Raspberry Pi 3, 4 or 5. The lab's are 4s. In Raspberry Pi Imager:

| Setting | Value |
|---|---|
| OS | Raspberry Pi OS Lite (64-bit) |
| Hostname | `emec-ams-` and the machine, like `emec-ams-lathe-1` or `emec-ams-mill-a` |
| Localisation | Leave the defaults. The setup script sets the time zone |
| User | `emec`, with a password of your choice |
| Wi-Fi | None |
| SSH | On, with the dashboard VM's public key (`~/.ssh/id_ed25519.pub` on the VM) |
| Raspberry Pi Connect | Off |

The VM signs in to every Pi with that key, to pull its logs each night.

## Setting up a Pi

Log in to the Pi as `emec`, then run:

```
curl -O https://raw.githubusercontent.com/zacjcsu/emec-ams-client/refs/heads/main/emecamssetup.sh
bash emecamssetup.sh
```

It asks everything first, then runs on its own. Have these ready:

- The database password. Every Pi uses the same one.
- The machine ID. The script lists the machines on the dashboard.
- For a CNC that shares programs, a password for the control to sign in with.

If it ends with "Reboot required", run `sudo reboot`. The app starts after the reboot.

Running it again is safe. Each question then defaults to what the Pi has now.

To set up without questions, pass the answers as environment variables and add `--yes`. The list is at the top of
`emecamssetup.sh`. Run the first line on its own, so the password stays out of the shell history:

```
read -rs -p "Database password: " DB_PASS; export DB_PASS; echo
```
```
MACHINE_ID=lathe-001 bash emecamssetup.sh --yes
```
