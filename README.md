# Bookloader

**Send files from Linux to an iPhone over USB — no Apple ID, no Wi‑Fi, no
network tethering.**

Bookloader is a small Python/tkinter GUI that delivers audiobooks and music
(`.m4a`, `.m4b`, and other audio formats) plus `.txt` and `.pdf` files to an
iPhone using only a USB cable. Files land in **Files → On My iPhone →
Downloads**, where the built-in iOS player plays `.m4a`/`.m4b` directly.

Verified on an iPhone SE (1st generation), iOS 15.8.8, with the VM running
Linux and the phone attached to the VM over USB.

## How it works

Bookloader talks to mobile Safari through **Web Inspector over USB**
(usbmuxd). A local CDP bridge serves a small branded status page and delivers
each file as a Blob with a download link. Safari turns that into a real iOS
download prompt — the only thing you do on the phone is tap **Download**.
There is no cloud service, no internet connection, and no Apple ID involved.

The Safari page shows a live "USB connected" indicator, a progress bar, and a
per-file list that turns yellow while preparing and green when done, so you
can see the transfer is working from either screen.

## Quick start

1. On the iPhone, enable **Settings → Safari → Advanced → Web Inspector**.
2. Connect the iPhone with a USB cable and unlock it.
3. Install dependencies (once):
   ```sh
   ./install_deps.sh
   ```
4. Run the app:
   ```sh
   ./run.sh
   ```
   If the phone is attached to a VM rather than your desktop, run the app
   inside the VM over X forwarding:
   ```sh
   ssh -X iriseth@192.168.122.50
   cd ~/opc/bookloader
   ./run.sh
   ```
   File dialogs browse the **VM's** filesystem. To get files from the host
   into the VM, copy them into the watched inbox:
   ```sh
   scp /path/on/host/book.m4b iriseth@192.168.122.50:~/opc/bookloader/inbox/
   ```
   New files in `inbox/` appear in the list automatically within seconds.
5. Add files with **Add files…** / **Add folder…**, or use the inbox. Then:
   - **Send all via Safari** — sends every file in the list.
   - **Send selected via Safari** — sends only the Ctrl/Shift-clicked rows.
   - **Send folder…** — pick a folder and send its whole subtree directly.
6. Files are delivered **one at a time**. The iOS download prompt is modal, so
   sending several at once would block later files and exhaust an older
   phone's memory with multiple large Blobs. Choose a mode in **Options**:
   - **Auto-advance** (default): the app waits N seconds between files (set
     the delay, 2–60 s, in Options). Tap **Download** on the phone as each
     prompt appears; nothing to press in the app.
   - **Manual**: uncheck Auto-advance; for each file, tap Download on the
     phone, then press **✓ Confirmed on phone — next file** in the app.
7. Open **Files → On My iPhone → Downloads** and play the file there.

## Features

- **Custom file picker** (Add files…): a wide in-app dialog listing one file
  per line as `name    size` in a single column, with Ctrl/Shift multi-select
  (or Select all/none), folder navigation, and an option to show all files.
- **Whole-folder subtree scanning** and individual file selection.
- **Multi-select** with Ctrl/Shift-click. Selecting any row greys out
  **Send all** so you cannot accidentally send the whole list when you meant
  to send only the selected files.
- **Detailed file list**: one filename + filesize per line.
- **Live progress bar** and timestamped log, mirrored on the Safari page.
- **Bookcoder-style naming** (optional): **Prefix names with folder** turns
  chapter files into `My Book - 01 - Chapter 01.m4a`, which keeps chapters
  grouped and sorted in iOS's flat Downloads folder.
- **Auto-advance** with configurable delay, or manual confirmation.
- **Watched inbox**: drop files into `inbox/` from the host with `scp` and
  they are picked up automatically.
- **Status page in Safari**: branded background, USB-connection badge, live
  progress, and a per-file list showing preparing/active/done state.

## Requirements

- Linux with Python 3, `python3-tk`, `usbmuxd`, and `libimobiledevice-utils`.
- An iPhone connected by USB with **Web Inspector** enabled.
- `pymobiledevice3` and `websocket-client` (installed into `.venv` by
  `install_deps.sh` or automatically by `run.sh` on first launch).
- No Apple ID, no Wi‑Fi, no SIM, no Personal Hotspot, no internet at use time.

`bookloader.py` bundles a Tk runtime in `.tk/` for systems whose Python lacks
`libtk8.6.so`; `run.sh` configures it automatically. The `.venv/` and `.tk/`
directories are generated locally and are **not** part of this repository —
run `install_deps.sh` to create them.

## Limitations

- Files always land in the flat **Downloads** folder. iOS does not let Safari
  choose a subfolder or write into the Music/Books library. Use filename
  prefixes (option above) to group and order chapters.
- The built-in player plays M4A/M4B but ignores chapter metadata and does not
  remember playback position. A dedicated player can show chapters, but needs
  an App Store install with an Apple ID.
- Raw AFC copies into `books`/`music`/`downloads` are deliberately **not**
  offered: iOS apps read a private library database, so those files never
  appear as playable items. Safari delivery lets iOS save the file itself.
- There is no Linux tool that writes into the stock Music library on iOS 15
  without a jailbreak.
- Each file requires one **Download** tap on the phone — iOS's download
  approval cannot be automated away. Auto-advance spaces the prompts out so
  you can keep up.

## Troubleshooting

- **"Page did not load in Safari":** the app resets Safari's page before each
  send. If it still fails, make sure Web Inspector is enabled, unlock the
  phone, and retry.
- **Stale CDP server / connection errors:**
  `pkill -f '[p]ymobiledevice3 webinspector cdp'`, then resend.
- **No download prompt:** confirm Web Inspector is on and Safari is open; the
  app auto-clicks the download link, so only the iOS prompt needs a tap.
- **Empty file dialog:** it browses the VM, not the host — use `scp` into
  `inbox/`.
- **Tk / `libtk8.6.so` errors:** run `./install_deps.sh` and launch through
  `./run.sh`, which sets up the bundled Tk runtime.

## License

GPL-3.0-or-later.
